"""Find every code site that loads a 32-bit literal from its pool, then show it.

A peripheral base like 0x04280000 is never an immediate; the code loads it from
a literal pool with `ldr rN,[pc,#imm]` (A32) or `ldr rN,[pc,#imm]` /
`ldr.w rN,[pc,#imm]` (T32) and then indexes off it. Grepping the image for the
word only finds the pool, so this walks back from each pool hit to the
instruction that consumes it.

  python poolref.py 0x04280000
  Z6II_IMAGE=sla Z6II_AFTER=30 python poolref.py 0x0428002c

Z6II_AFTER is how many instructions to print per hit; every line that mentions
the loaded register is marked with a leading '>'.
"""
import os
import re
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs

IMAGES = {
    "mas": r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700",
    "sla": r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700",
}
BASE = 0x10020000
THE_END = BASE + 0x2400000

A32 = Cs(CS_ARCH_ARM, CS_MODE_ARM)
T32 = Cs(CS_ARCH_ARM, CS_MODE_THUMB)


def a32_refs(blob, pool_va):
    """Instruction addresses whose A32 pool load resolves to pool_va."""
    out = []
    for imm in range(0, 4096, 4):
        for addr in (pool_va - 8 - imm, pool_va - 8 + imm):
            if addr < BASE or addr % 4 or addr + 4 > THE_END:
                continue
            off = addr - BASE
            w = int.from_bytes(blob[off:off + 4], "little")
            if (w & 0x051F0000) != 0x051F0000 or (w & 0xfff) != imm:
                continue
            rd = (w >> 12) & 0xf
            u = (w >> 23) & 1
            out.append((addr, rd, "a32"))
    return out


def t32_refs(blob, pool_va):
    """Same for T32, both the 16-bit and the 32-bit literal form."""
    out = []
    for addr in range(pool_va - 4096 - 4, pool_va + 1, 2):
        if addr < BASE or addr + 4 > THE_END:
            continue
        off = addr - BASE
        hw1 = int.from_bytes(blob[off:off + 2], "little")
        hw2 = int.from_bytes(blob[off + 2:off + 4], "little") \
            if off + 4 <= len(blob) else 0
        pc4 = (addr + 4) & ~3
        if (hw1 & 0xf800) == 0x4800:
            imm = (hw1 & 0xff) * 4
            if pc4 + imm == pool_va:
                out.append((addr, (hw1 >> 8) & 7, "t16"))
        if (hw1 & 0xffdf) == 0xf8df:
            imm = hw2 & 0xfff
            u = (hw1 >> 7) & 1
            if pc4 + (imm if u else -imm) == pool_va:
                out.append((addr, (hw2 >> 12) & 0xf, "t32"))
    return out


def main():
    value = int(sys.argv[1], 16)
    after = int(os.environ.get("Z6II_AFTER", "24"))
    path = IMAGES[os.environ.get("Z6II_IMAGE", "mas")]
    blob = open(path, "rb").read()

    pools = [m.start() + BASE for m in
             re.finditer(re.escape(value.to_bytes(4, "little")), blob)]

    sites = []
    for p in pools:
        sites += a32_refs(blob, p)
        sites += t32_refs(blob, p)
    sites.sort()

    if not sites:
        print("no pool load for 0x%08x (%d raw word hits)" % (value, len(pools)))
        return

    print("0x%08x: %d pool hit(s), %d load site(s)" %
          (value, len(pools), len(sites)))
    for addr, rd, kind in sites:
        md = A32 if kind == "a32" else T32
        start = addr - BASE
        code = blob[start:start + after * 4 + 16]
        print("\n--- %s site 0x%08x -> r%d ---" % (kind, addr, rd))
        name = "r%d" % rd
        n = 0
        for insn in md.disasm(code, addr | (1 if kind != "a32" else 0)):
            mark = "> " if re.search(r"\b%s\b" % name, insn.op_str) else "  "
            print("%s0x%08x: %-8s %s %s" % (mark, insn.address,
                                            insn.bytes.hex(), insn.mnemonic,
                                            insn.op_str))
            n += 1
            if n >= after:
                break


if __name__ == "__main__":
    main()
