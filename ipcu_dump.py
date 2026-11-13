#!/usr/bin/env python3
"""Dump the inter-core RPC / IPCU string surfaces and fix sla base. Read-only."""
import re, sys
from collections import Counter

M, S = "extracted/eg1850_mas_01700", "extracted/eg1850_sla_01700"
mas, sla = open(M, "rb").read(), open(S, "rb").read()
STR = re.compile(rb"[ -~]{5,72}")


def show(img, off, span, tag, step=1):
    lo, hi = max(0, off - span), min(len(img), off + span)
    out = []
    for m in STR.finditer(img[lo:hi]):
        out.append(f"{lo + m.start():#X}:{m.group(0).decode()}")
    print(f"\n### {tag}  (window {lo:#X}..{hi:#X}, {len(out)} strings)")
    for i in range(0, len(out), step):
        print("   " + " | ".join(out[i:i + step]))


show(mas, 0x216E85F, 0x900, "mas: CArm926IpcuService_* RPC method names")
show(mas, 0x20B0C64, 0x700, "mas: IpcuDriver.c assert/param strings")
show(mas, 0x20B18D2, 0x500, "mas: AM926SR0 / EFS / EFR service names")
show(mas, 0x76ACF6, 0x500, "mas: uppercase IPCU register/command block")
show(sla, 0x147EF6, 0x500, "sla: uppercase IPCU register/command block")
show(mas, 0x21CBCAD, 0x400, "mas: Master/Slave naming block A")
show(mas, 0x2251A31, 0x400, "mas: Master/Slave naming block B")
show(sla, 0x85270A, 0x400, "sla: SLAVE id block")
show(mas, 0x242F206, 0x400, "mas: SLAVE id block")
show(mas, 0x210DA53, 0x300, "mas: MASTER block")
show(mas, 0x37E0E8, 0x300, "mas: EG1850 SoC naming block")

MAS_BASE, MAS_END = 0x10000000, 0x10000000 + len(mas)
c = Counter()
for x in memoryview(sla).cast("I"):
    if MAS_END - (1 << 22) <= x < MAS_END + (8 << 20):
        c[x >> 16] += 1
print("\n### literals inside sla near/after mas_end")
for k, n in sorted(c.items()):
    print(f"   0x{k:05X}0000  x{n:<5} delta_vs_mas_end = {(k<<16)-MAS_END:+,}")
