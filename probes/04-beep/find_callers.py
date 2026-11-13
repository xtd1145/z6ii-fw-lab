"""Find every T32 branch that targets a given address, and show the call site.

The beep routine is only safe to call if we feed it what its other callers feed
it, so the call sites matter more than the routine's own body. Thumb branches
are PC-relative and densely encoded, so this walks every 2-byte-aligned
halfword pair that looks like B.W/BL (bits 15:11 = 11110) and decodes the
target with the same formula the patcher uses.

  python work/beep_fix/find_callers.py 0x10768b6e
  Z6II_FILE=dist/beep4/EG185070.BIN python work/beep_fix/find_callers.py 0x1002d884
"""
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = 0x10020000
DEFAULT = ROOT / "extracted" / "eg1850_mas_01700"


def sign(value, bits):
    top = 1 << (bits - 1)
    return (value ^ top) - top


def target_of(hw1, hw2, addr):
    s = (hw1 >> 10) & 1
    imm10 = hw1 & 0x3FF
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    imm11 = hw2 & 0x7FF
    raw = (s << 24) | ((1 ^ j1 ^ s) << 23) | ((1 ^ j2 ^ s) << 22) | \
          (imm10 << 12) | (imm11 << 1)
    return addr + 4 + sign(raw, 25)


def main():
    want = int(sys.argv[1], 16)
    path = Path(os.environ.get("Z6II_FILE", DEFAULT))
    blob = path.read_bytes()
    hits = []
    for off in range(0, len(blob) - 4, 2):
        hw1 = blob[off] | blob[off + 1] << 8
        if (hw1 & 0xF800) != 0xF000:
            continue
        hw2 = blob[off + 2] | blob[off + 3] << 8
        addr = BASE + off
        if target_of(hw1, hw2, addr) == want:
            hits.append((addr, hw2 >> 14))
    print("%s: %d branch(es) to 0x%08x" % (path.name, len(hits), want))
    for addr, kind in hits:
        print("  0x%08x  %s" % (addr, "bl" if kind == 3 else "b.w"))


if __name__ == "__main__":
    main()
