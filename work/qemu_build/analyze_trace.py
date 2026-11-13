#!/usr/bin/env python3
"""Summarise a nikon-z6ii QEMU trace.

The machine models RAM plus logged stubs only, so what matters is which
registers the boot code touched, in what order, and where it stopped. That list
is the specification for the real peripheral models.

hw/misc/unimp.c logs "<name>: unimplemented device write (size N, offset 0x..,
value 0x..)" with the offset relative to the device, so absolute addresses are
reconstructed from the bases in hw/arm/nikon_z6ii.c.

Usage: python analyze_trace.py [trace.log]
"""
import collections
import re
import sys

BASE = {
    "z6ii.peri0": 0x04000000,
    "z6ii.peri1": 0x04010000,
    "z6ii.peri2": 0x20000000,
    "z6ii.peri3": 0x80000000,
}
LINE = re.compile(
    r"^(?P<dev>[\w.\-]+): unimplemented device (?P<kind>read|write)\s+"
    r"\(size (?P<size>\d+), offset 0x(?P<off>[0-9a-f]+)"
    r"(?:, value 0x(?P<val>[0-9a-f]+))?\)")

path = sys.argv[1] if len(sys.argv) > 1 else \
    r"D:\z6ii_fw_lab\work\qemu_build\run\trace.log"

hits = collections.Counter()
first = {}
order = []
last_value = {}
sizes = collections.Counter()
unknown_dev = collections.Counter()
in_blocks = []
pending = None
tail_int = []

with open(path, "r", errors="replace") as fh:
    for lineno, line in enumerate(fh, 1):
        m = LINE.match(line)
        if m:
            dev = m["dev"]
            off = int(m["off"], 16)
            if dev not in BASE:
                unknown_dev[dev] += 1
                continue
            addr = BASE[dev] + off
            key = (dev, off & ~3)
            hits[key] += 1
            sizes[(m["kind"], m["size"])] += 1
            if m["val"]:
                last_value[key] = int(m["val"], 16)
            if key not in first:
                first[key] = (lineno, addr, m["kind"])
                order.append(key)
            continue
        if line.startswith("IN:"):
            pending = []
            in_blocks.append(pending)
            if len(in_blocks) > 400:
                in_blocks.pop(0)
            continue
        if pending is not None:
            if line.startswith(" /") or line[:1] in (" ", "\t"):
                pending.append(line.strip())
            else:
                pending = None
        if "exception" in line.lower() or "exccode" in line:
            tail_int.append((lineno, line.rstrip()))

total = sum(hits.values())
print("device accesses logged: %d over %d distinct register words" % (total, len(hits)))
if unknown_dev:
    print("NOTE devices not in BASE: %s" % dict(unknown_dev))

print("\n--- touch order (first 45) ---")
for key in order[:45]:
    dev, off = key
    ln, addr, kind = first[key]
    val = last_value.get(key)
    print("  %-11s abs=%08x  +0x%06x  %-5s x%-3d value=%s (line %d)"
          % (dev, addr, off, kind, hits[key],
             "0x%08x" % val if val is not None else "-", ln))

print("\n--- busiest ---")
for (dev, off), n in hits.most_common(25):
    print("  %-11s +0x%06x : %-5d abs=%08x last=%s"
          % (dev, off, n, BASE[dev] + off,
             "0x%08x" % last_value[(dev, off)] if (dev, off) in last_value else "-"))

print("\n--- access sizes ---")
for k, n in sorted(sizes.items()):
    print("  %s %s byte : %d" % (k[0], k[1], n))

print("\n--- last exception-ish lines ---")
for ln, text in tail_int[-10:]:
    print("  line %d: %s" % (ln, text[:160]))

print("\n--- last executed TB ---")
if in_blocks:
    for l in in_blocks[-1][-30:]:
        print("  " + l)
else:
    print("  (no IN: blocks - check that -d in_asm was passed and the log is not empty)")
