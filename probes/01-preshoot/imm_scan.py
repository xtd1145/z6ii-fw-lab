#!/usr/bin/env python3
"""Find code sites that materialise the shutter switch mask constants.

ID_SW0_RELEASE_HALF = 0x00080000, ID_SW0_RELEASE_ON = 0x80000000 (see the
name+bit table at file 0x918c00).  Logs every instruction whose immediate
operand equals one of them, with a little context afterwards.
"""
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM

BASE = 0x10000000
MAS = r"D:\z6ii_fw_lab\all_modules\Z6_2_0170\eg1850_mas_01700"
WANT = {0x00080000: "REL_HALF", 0x80000000: "REL_ON", 0x00400000: "AF_ON",
        0x00800000: "FLASH", 0x00200000: "AF_ON_VPOS"}
KEEP = {"and", "ands", "orr", "orrs", "tst", "teq", "bic", "bics",
        "mov", "movs", "movw", "eor", "eors", "add", "adds", "cmp"}


def _chunked(md, data, lo, hi, chunk=0x200000):
    off = lo
    while off < hi:
        end = min(off + chunk, hi)
        for ins in md.disasm(data[off:end], BASE + off):
            yield ins
        off = end


def main():
    data = open(MAS, "rb").read()
    lo = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0
    hi = int(sys.argv[2], 0) if len(sys.argv) > 2 else len(data)
    thumb = "--arm" not in sys.argv
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
    md.detail = True
    md.skipdata = True
    hits = []
    for ins in _chunked(md, data, lo, hi):
        if ins.mnemonic not in KEEP:
            continue
        for op in ins.operands:
            if op.type == ARM_OP_IMM and op.imm in WANT:
                hits.append((ins.address, WANT[op.imm], ins.mnemonic, ins.op_str))
                break
    for addr, tag, mn, ops in hits:
        print("0x%08x  %-9s %-7s %s" % (addr, tag, mn, ops))
    print("hits:", len(hits))


if __name__ == "__main__":
    main()
