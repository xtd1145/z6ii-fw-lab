"""Validate a Nikon Z6 II NOR dump and export the three boot parameter groups.

The C1.70 update package carries no factory parameters: the master scatter table
(0x10034040..0x1003413c) only initialises RAM regions, and the manifest lists
five modules, none of them parameter data. The groups the boot store wants live
only in the 64 MiB NOR, so this tool exists for the moment a dump arrives.

  python nor_import.py D:/dumps/NOR_64MiB.bin
  python nor_import.py --scan candidate.bin      # find the groups at any base
  python nor_import.py D:/dumps/NOR.bin --dest .local/parameters_real

What counts as a real group: the record ends with the ASCII footer 'ENDM' and
has the exact length the Z62 ROM table selects (17/18/32 below). Anything else
is reported, not silently accepted.
"""
import argparse
import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

NOR_SIZE = 0x4000000
ROM_BASE = 0x10000000
GROUPS = ((17, 0x132c0000, 0x20000),
          (18, 0x132e0000, 0xa0000),
          (32, 0x13e80000, 0x180000))
FOOTER = b"ENDM"


def entropy(data):
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    return -sum((c / total) * math.log2(c / total)
                for c in counts if c)


def check(path, number, address, size, offset):
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(size)
    if len(data) != size:
        return None, "short read (%d of %d bytes)" % (len(data), size)
    if data[-4:] != FOOTER:
        return None, ("footer is %r, expected %r" % (data[-4:], FOOTER))
    if not data.strip(b"\0"):
        return data, "blank (all zero)"
    return data, "ok entropy=%.2f nonzero=%d" % (
        entropy(data), len(data.strip(b"\0")))


def import_nor(path, dest):
    size = path.stat().st_size
    if size != NOR_SIZE:
        print("warning: %s is %d bytes, expected %d (64 MiB)"
              % (path, size, NOR_SIZE))
    dest.mkdir(parents=True, exist_ok=True)
    report = []
    ok = True
    for number, address, group_size in GROUPS:
        offset = address - ROM_BASE
        data, status = check(path, number, address, group_size, offset)
        if data is None:
            ok = False
            print("group %-2d @%#010x: REJECTED - %s" % (number, address, status))
            continue
        name = dest / ("group%d.bin" % number)
        name.write_bytes(data)
        print("group %-2d @%#010x: %s  sha256=%s -> %s"
              % (number, address, status,
                 hashlib.sha256(data).hexdigest()[:16], name))
        report.append(dict(group=number, address=hex(address), size=group_size,
                           sha256=hashlib.sha256(data).hexdigest(), note=status))
    if not ok:
        print("\nNot a usable NOR. Try --scan to locate the groups at another "
              "base, or dump the complete 64 MiB.")
        return 1
    (dest / "parameters.json").write_text(json.dumps(
        dict(format=1, experimental=False, factory_calibration=True,
             source=str(path), source_sha256=hashlib.sha256(
                 path.read_bytes()).hexdigest(),
             storage_groups=report), indent=2) + "\n", encoding="utf-8")
    aux = ",,".join("%s/group%d.bin@%#x" % (dest, n, a) for n, a, _ in GROUPS)
    print("\nUsable. Run the board with real parameters:")
    print('  python work/qemu_build/board_probe.py --aux "%s" ...' % aux)
    return 0


def scan(path):
    """Look for an ENDM-terminated group of the right size at any base.

    Every ENDM in the file is a candidate end marker (bytes.find keeps this
    cheap), and a group is reported when the span of its exact length starts
    with real data rather than filler.
    """
    blob = path.read_bytes()
    total = 0
    for number, _, size in GROUPS:
        found = []
        end = blob.find(FOOTER)
        while end != -1 and len(found) < 8:
            start = end - size + 4
            if start >= 0:
                body = blob[start:end]
                if body.strip(b"\0") and entropy(body) > 0.05:
                    found.append((start, entropy(body), body.count(b"\0")))
            end = blob.find(FOOTER, end + 1)
        total += len(found)
        print("group %d (size %#x): %d candidate(s)" % (number, size, len(found)))
        for start, ent, zeros in found[:8]:
            print("   @%#010x  entropy=%.2f zero_bytes=%d" % (start, ent, zeros))
    return 0 if total else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="candidate 64 MiB NOR dump")
    ap.add_argument("--dest", default=".local/parameters_real")
    ap.add_argument("--scan", action="store_true")
    args = ap.parse_args()
    path = Path(args.image)
    if args.scan:
        return scan(path)
    return import_nor(path, Path(args.dest))


if __name__ == "__main__":
    sys.exit(main())
