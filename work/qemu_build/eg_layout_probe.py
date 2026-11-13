#!/usr/bin/env python3
"""Locate reset vector, boot code and candidate RAM/peripheral windows in the
eg master (mas) and eg slave (sla) images, so the QEMU machine memmap can be
derived from evidence instead of guesswork.

ARM B imm24 is a *word* offset: target = (pc + 8) + imm24 * 4.
"""
import sys, collections
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB

ROOT = r"D:\z6ii_fw_lab"
MAS = ROOT + r"\extracted\eg1850_mas_01700"
SLA = ROOT + r"\extracted\eg1850_sla_01700"

def disasm(blob, base, count, mode=CS_MODE_ARM):
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)
    if mode == CS_MODE_THUMB:
        md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    md.detail = False
    return list(md.disasm(blob[base:base + count * 4], 0x10000000 + base, count))

def vec_table(blob, name):
    print(f"\n=== {name}: exception vector table (assuming load base 0) ===")
    for off, label in [(0x00, "Reset"), (0x04, "Undef"), (0x08, "SWI"), (0x0c, "PAbt"),
                       (0x10, "DAbt"), (0x14, "IRQ"), (0x18, "FIQ"), (0x1c, "Res")]:
        w = int.from_bytes(blob[off:off + 4], "little")
        if (w >> 24) == 0xEA:
            tgt = (off + 8) + (w & 0xFFFFFF) * 4
            print(f"  {off:#06x} {label:<6} B -> {tgt:#010x}")
        elif (w >> 24) in (0xE5, 0x15, 0x5D) or (w & 0x0FB00000) == 0x05100000:
            print(f"  {off:#06x} {label:<6} ldr-ish word={w:#010x}")
        else:
            print(f"  {off:#06x} {label:<6} {w:#010x}  (cond={w>>28:#x} op={(w>>25)&7:#x})")

def boot_code(blob, name, start, n=40):
    print(f"\n=== {name}: disasm @{start:#x} ===")
    ins = disasm(blob, start, n)
    for i in ins:
        print(f"  {start + (i.address - 0x10000000):#010x}  {i.mnemonic:<8} {i.op_str}")

def addr_hist(blob, name, lo=0, hi=None):
    """Every 4-byte-aligned word that looks like a pointer into the image, plus
    words that look like a peripheral/RAM register address."""
    hi = hi or len(blob)
    print(f"\n=== {name} ({len(blob):#x} bytes): 32-bit value classification ===")
    inside = collections.Counter()
    regwin = collections.Counter()
    n = (min(hi, len(blob)) - lo) // 4
    for k in range(n):
        off = lo + k * 4
        w = int.from_bytes(blob[off:off + 4], "little")
        if 0x2000 <= w < len(blob) and w % 4 == 0:
            inside[w // 0x100000] += 1
        if w and not (w >> 31) and (w & 0xFFFF) == 0:          # e.g. 0x90010000 style
            regwin[w] += 1
        if 0x0100_0000 <= w <= 0x0FFF_FFFF:                     # plausible SRAM/regs
            inside[w // 0x100000] += 1
    print("  pointer-ish targets, per 1 MiB bucket (top 18):")
    for b, c in inside.most_common(18):
        print(f"    {b*0x100000:#010x} .. {b*0x100000+0xFFFFF:#010x}  count={c}")
    print("  low-16-bits-zero words (register-base candidates, top 15):")
    for w, c in regwin.most_common(15):
        print(f"    {w:#010x}  count={c}")

def main():
    for path, name in [(MAS, "mas"), (SLA, "sla")]:
        blob = open(path, "rb").read()
        print(f"\n########## {name}: {path}  len={len(blob):#x} ##########")
        vec_table(blob, name)
    mas = open(MAS, "rb").read()
    sla = open(SLA, "rb").read()
    # reset target from mas vector 0
    w0 = int.from_bytes(mas[0:4], "little")
    if (w0 >> 24) == 0xEA:
        boot_code(mas, "mas reset handler", (w0 & 0xFFFFFF) * 4 + 8, 30)
    addr_hist(mas, "mas", 0, len(mas))
    addr_hist(sla, "sla", 0, len(sla))

if __name__ == "__main__":
    sys.exit(main())
