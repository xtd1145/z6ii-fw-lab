"""Decode the PTP operation table out of a runtime RAM dump.

The table is built at runtime, so the disk image has zeros there; get a snapshot
first with `ram_dump.py --regions 0x119f0000:0x00010000`, then parse it here.

Entry layout (28 bytes), recovered from 0x104a95a8 (stride 28, u16 code, stop at
code == 0) and 0x104a95d8 (uses +8 as the handler argument and +12 as the
function pointer):

  +0  u16 code
  +8  u32 arg
  +12 u32 handler   (0 and 0x104c95f3 both mean "does nothing")

  python ptp_table.py
  python ptp_table.py --dump run/ptp/ram_119f0000_00010000.bin \
      --dump-base 0x119f0000 --table 0x119f496c
"""
import argparse
import os
import struct
import sys

DEFAULT_DUMP = "run/ptp/ram_119f0000_00010000.bin"
DEFAULT_BASE = 0x119F0000
DEFAULT_TABLE = 0x119F496C
STUB = 0x104C95F3
STRIDE = 28


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=DEFAULT_DUMP)
    ap.add_argument("--dump-base", type=lambda x: int(x, 0), default=DEFAULT_BASE)
    ap.add_argument("--table", type=lambda x: int(x, 0), default=DEFAULT_TABLE)
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    path = args.dump
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.dump)
    if not os.path.exists(path):
        raise SystemExit("missing dump %s - run ram_dump.py first" % args.dump)
    data = open(path, "rb").read()

    off = args.table - args.dump_base
    if not (0 <= off < len(data)):
        raise SystemExit("table 0x%08x outside dump window" %
                         args.table)

    rows = []
    a = off
    for _ in range(args.limit):
        if a + STRIDE > len(data):
            break
        code = struct.unpack_from("<H", data, a)[0]
        if code == 0:
            break
        arg = struct.unpack_from("<I", data, a + 8)[0]
        fn = struct.unpack_from("<I", data, a + 12)[0]
        rows.append((args.table + (a - off), code, arg, fn))
        a += STRIDE

    print("table 0x%08x: %d entries" % (args.table, len(rows)))
    if not rows:
        print("  empty - the table is built at runtime, capture a later snapshot")
        return 1

    real = [(c, f) for _, c, _, f in rows if f not in (0, STUB)]
    print("  live handlers: %d, stubs: %d" % (len(real), len(rows) - len(real)))
    print()
    print("  %-6s %-10s %s" % ("code", "handler", "note"))
    for addr, code, arg, fn in rows:
        note = ""
        if fn == 0:
            note = "null - executes nothing"
        elif fn == STUB:
            note = "shared stub"
        print("  %04x   %08x   %s" % (code, fn, note))

    print()
    standard = [c for _, c, _, _ in rows if 0x1000 <= c < 0x2000]
    capture = [c for _, c, _, _ in rows if 0x9200 <= c <= 0x9210]
    print("  standard PTP ops (0x1xxx): %d" % len(standard))
    print("  capture ops (0x9200-0x9210): %d" % len(capture))
    if not standard and not capture:
        print("  -> this table only registers vendor codes; 0x9207 InitiateCapture")
        print("     is not dispatched here")
    return 0


if __name__ == "__main__":
    sys.exit(main())
