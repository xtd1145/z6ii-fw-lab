#!/usr/bin/env python3
"""Dump the short-descriptor table embedded in eg1850_mas_01700.

The machine needs a PA layout and the only evidence is the firmware's own
translation table: TTBR0 = 0x10030000 and the table sits at file offset
0x10000, so the module base is 0x10020000 and entry i covers VA i * 1MiB.

PA[31:20] = word[31:20] in every short-descriptor layout, so the PA layout below
is layout-independent. Domain/XN/AP positions differ between the ARMv6 and the
ARMv7 encodings, so both are printed; QEMU (target/arm/ptw.c get_phys_addr_v6)
decodes domain from bits[8:5], XN from bit 4 and AP from bits[11:10]|bit15.
"""
import struct
import sys

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
TBL_OFF = 0x10000
TBL_VA = 0x10030000
BASE = TBL_VA - TBL_OFF   # 0x10020000

blob = open(MAS, "rb").read()
print("module size  : 0x%x (%d bytes)" % (len(blob), len(blob)))
print("base         : 0x%08x .. 0x%08x" % (BASE, BASE + len(blob)))


def ent(i):
    return struct.unpack_from("<I", blob, TBL_OFF + i * 4)[0]


assert ent(0) == 0, hex(ent(0))
assert (ent(256) & 0xFFF00000) == 0x10000000, hex(ent(256))

print("\n== sections covering the module (VA 0x10000000 .. 0x12700000) ==")
for i in range(256, 296):
    v = ent(i)
    va = i << 20
    if v & 3 == 2:
        pa = v & 0xFFF00000
        print("VA %08x -> PA %08x  word=%08x  qemu:dom=%d xn=%d ap=%d "
              "armv7:dom=%d cb=%d" % (
                  va, pa, v, (v >> 5) & 0xF, (v >> 4) & 1,
                  ((v >> 10) & 3) | ((v >> 13) & 4), (v >> 16) & 0xF,
                  (v >> 18) & 3))
    else:
        print("VA %08x          word=%08x  type=%d (not a section)" % (va, v, v & 3))

print("\n== all runs (grouped by contiguous PA and identical word attributes) ==")
i = 0
while i < 4096:
    v = ent(i)
    if v & 3 != 2:
        i += 1
        continue
    pa0 = v & 0xFFF00000
    n = 1
    while i + n < 4096:
        vn = ent(i + n)
        if vn & 3 != 2 or (vn & 0xFFF00000) != pa0 + n * 0x100000 or vn != v:
            break
        n += 1
    print("VA %08x..%08x -> PA %08x..%08x (%5.1fM) word=%08x "
          "qemu dom=%d xn=%d ap=%d | armv7 dom=%d cb=%d nG=%d" % (
              i << 20, ((i + n) << 20) - 1, pa0, pa0 + n * 0x100000 - 1,
              n * 1.0, v, (v >> 5) & 0xF, (v >> 4) & 1,
              ((v >> 10) & 3) | ((v >> 13) & 4), (v >> 16) & 0xF,
              (v >> 18) & 3, (v >> 13) & 1))
    i += n
sys.exit(0)
