"""Inventory every ARM coprocessor (MRC/MCR p15) access in a loaded eg module.

QEMU models the architectural registers but not the vendor ones, so each
unmodelled read silently returns 0. The boot descriptor's two table pointers
come from `mrc p15, #4, Rd, c15, c0, #0`; this lists that register plus every
other p15 operand combination the image touches, with the sites and the code
following each one.

  python cp15scan.py                 # mas
  Z6II_IMAGE=sla python cp15scan.py
  python cp15scan.py "c15, c0"       # only operand sets containing this
  Z6II_FILE=... Z6II_VA=0 python cp15scan.py   # any raw blob
"""
import os
import re
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs

IMAGES = {
    "mas": r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700",
    "sla": r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700",
    "ex": r"D:\z6ii_fw_lab\extracted\ex1850_012000.bi",
    "vr": r"D:\z6ii_fw_lab\extracted\vr1850_010100.bi",
    "tpj": r"D:\z6ii_fw_lab\extracted\_tpj01_v140.bin",
}
MAS_VA = int(os.environ.get("Z6II_VA", "0x10020000"), 0)
CTX = int(os.environ.get("Z6II_CTX", "6"))
FOCUS = sys.argv[1] if len(sys.argv) > 1 else None


def main():
    path = os.environ.get("Z6II_FILE") or IMAGES[os.environ.get("Z6II_IMAGE", "mas")]
    blob = open(path, "rb").read()
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    groups = {}
    for off in range(0, len(blob) - 4, 4):
        w = int.from_bytes(blob[off:off + 4], "little")
        if (w & 0x0F000000) != 0x0E000000:
            continue
        site = None
        for insn in md.disasm(blob[off:off + 4], MAS_VA + off, count=1):
            if insn.mnemonic not in ("mrc", "mcr", "mrrc", "mcrr"):
                continue
            if "p15" not in insn.op_str:
                continue
            site = insn
        if site is None:
            continue
        key = re.sub(r"\br\d+\b", "Rd", site.op_str, count=1)
        groups.setdefault(key, []).append(MAS_VA + off)
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        sites = groups[key]
        if FOCUS and FOCUS not in key:
            continue
        print("== %-40s %d site(s)" % (key, len(sites)))
        for va in sites[:int(os.environ.get("Z6II_SHOW", "40"))]:
            off = va - MAS_VA
            print("  0x%08x: %s" % (va, blob[off:off + 4][::-1].hex()))
            for insn in md.disasm(blob[off + 4:off + 4 + 4 * CTX], va + 4):
                print("      0x%08x: %-8s %s %s" % (
                    insn.address, insn.bytes.hex(), insn.mnemonic, insn.op_str))
        print("")


if __name__ == "__main__":
    main()
