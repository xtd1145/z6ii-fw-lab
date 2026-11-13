"""Find code sites that materialise a 32-bit constant instead of loading it.

Thumb code builds peripheral bases with `movw rN,#lo16` / `movt rN,#hi16`, or
with the one-instruction `mov.w rN,#imm`, so a literal-pool grep misses them
entirely. Both forms are found here by prefiltering on the two halfwords that
carry the high bits, then confirming with capstone so the reported address is a
real instruction boundary.

  python immref.py 0x04280000
  Z6II_IMAGE=sla Z6II_AFTER=40 python immref.py 0x04280000
"""
import os
import re
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

IMAGES = {
    "mas": r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700",
    "sla": r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700",
}
BASE = 0x10020000
T32 = Cs(CS_ARCH_ARM, CS_MODE_THUMB)


def thumb_imm16(value, halfword, kind):
    """ThumbExpandImm for the modified-immediate forms mov.w/movt use."""
    if kind == "movt":
        return ((halfword >> 12) & 0xf) << 12 | ((halfword >> 10) & 1) << 11 | \
               ((halfword >> 8) & 1) << 10 | (halfword & 0xff)
    return value


def sites_of(blob, value):
    """Addresses where a T32 instruction (or a movw/movt pair) makes value."""
    lo = value & 0xffff
    hi = value >> 16
    out = []

    # movt rN,#hi : hw1 = 0xf2c0 | imm4<<... confirmed via capstone below, so a
    # loose byte prefilter is enough; false positives cost one decode.
    for m in re.finditer(b"\xc0\xf2", blob):
        off = m.start()
        if off % 2 or off + 4 > len(blob):
            continue
        addr = off + BASE
        hw2 = int.from_bytes(blob[off + 2:off + 4], "little")
        rd = (hw2 >> 8) & 0xf
        imm = ((hw2 >> 12) & 0xf) << 12 | ((hw2 >> 10) & 1) << 11 | \
              ((hw2 >> 8) & 1) << 10 | (hw2 & 0xff)
        if imm != hi:
            continue
        for insn in T32.disasm(blob[off:off + 4], addr | 1):
            if insn.mnemonic == "movt" and insn.op_str.startswith("r%d," % rd):
                out.append((addr, rd, "movt"))
                break

    for m in re.finditer(b"\x4f\xf0", blob):
        off = m.start()
        if off % 2 or off + 4 > len(blob):
            continue
        addr = off + BASE
        for insn in T32.disasm(blob[off:off + 4], addr | 1):
            if insn.mnemonic == "mov.w" and \
               insn.op_str.endswith("#0x%x" % value):
                out.append((addr, int(insn.op_str.split(",")[0][1:]), "mov.w"))
            break

    return out


def main():
    value = int(sys.argv[1], 16)
    after = int(os.environ.get("Z6II_AFTER", "20"))
    before = int(os.environ.get("Z6II_BEFORE", "8"))
    path = IMAGES[os.environ.get("Z6II_IMAGE", "mas")]
    blob = open(path, "rb").read()

    sites = sites_of(blob, value)
    print("0x%08x: %d immediate site(s)" % (value, len(sites)))
    for addr, rd, kind in sites:
        start = max(0, addr - BASE - before * 4)
        print("\n--- %s 0x%08x -> r%d ---" % (kind, addr, rd))
        name = "r%d" % rd
        for insn in T32.disasm(blob[start:start + (before + after) * 4 + 16],
                              (start + BASE) | 1):
            mark = "> " if re.search(r"\b%s\b" % name, insn.op_str) else "  "
            print("%s0x%08x: %-8s %s %s" % (mark, insn.address,
                                            insn.bytes.hex(), insn.mnemonic,
                                            insn.op_str))


if __name__ == "__main__":
    main()
