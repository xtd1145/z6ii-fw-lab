"""Disassemble a VA range out of a loaded eg module, with literal pools resolved.

mas and sla are both raw A32 images that QEMU loads at 0x10020000, so file
offset = VA - 0x10020000. Pool resolution matters here because the boot path
seeds its DDR globals through `ldr rX,[pc,#imm]` + `str`, and reading the raw
disassembly alone leaves the destination cells anonymous.

  python disrange.py 0x10103c18 0x10103d00
  Z6II_IMAGE=sla python disrange.py 0x10034f00 0x10034fc0

The post-boot cores run Thumb, so Z6II_MODE=thumb is required there; the two
literal encodings resolve the pool from Align(PC,4) rather than insn+8.
"""
import os
import re
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs

IMAGES = {
    "mas": r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700",
    "sla": r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700",
}
MAS_VA = 0x10020000


def main():
    lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
    path = IMAGES[os.environ.get("Z6II_IMAGE", "mas")]
    thumb = os.environ.get("Z6II_MODE") == "thumb"
    blob = open(path, "rb").read()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
    start = lo - MAS_VA
    code = blob[start:hi - MAS_VA]

    def word(va):
        off = va - MAS_VA
        if off < 0 or off + 4 > len(blob):
            return None
        return int.from_bytes(blob[off:off + 4], "little")

    for insn in md.disasm(code, lo | (1 if thumb else 0)):
        note = ""
        w = int.from_bytes(insn.bytes, "little")
        if thumb:
            if insn.mnemonic.startswith("ldr") and "[pc" in insn.op_str:
                imm = int(re.findall(r"#-?0x[0-9a-f]+|#-?\d+",
                                     insn.op_str)[-1].lstrip("#"), 0)
                pool = ((insn.address + 4) & ~3) + imm
                v = word(pool)
                if v is not None:
                    note = "   ; [0x%08x] = 0x%08x" % (pool, v)
        elif (w & 0x051F0000) == 0x051F0000:
            imm = w & 0xfff
            pool = insn.address + 8 + (imm if (w >> 23) & 1 else -imm)
            v = word(pool)
            if v is not None:
                note = "   ; [0x%08x] = 0x%08x" % (pool, v)
        print("0x%08x: %-8s %s %s%s" % (insn.address, insn.bytes.hex(),
                                        insn.mnemonic, insn.op_str, note))


if __name__ == "__main__":
    main()
