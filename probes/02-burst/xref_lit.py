"""xref_lit.py -- find the Thumb LDR [pc,#imm] that consumes a literal word."""
import struct, sys
import fw
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB


def find_ldr_to(name, lit_off, span=0x1000):
    b = fw.load(name)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    res = []
    start = max(0, lit_off - span)
    for ins in md.disasm(b[start:lit_off], fw.f2v(start)):
        if ins.mnemonic == "ldr" and "[pc" in ins.op_str:
            try:
                imm = int(ins.op_str.split("#")[-1].rstrip("]"), 0)
            except Exception:
                continue
            base = (ins.address + 4) & ~3
            tgt = base + imm
            if tgt == fw.f2v(lit_off):
                res.append((ins.address, ins.mnemonic, ins.op_str))
    return res


if __name__ == "__main__":
    lit = int(sys.argv[1], 0)
    name = sys.argv[2] if len(sys.argv) > 2 else "mas"
    print("literal vma 0x%08x (file 0x%x) in %s" % (lit, fw.v2f(lit), name))
    for a, m, o in find_ldr_to(name, fw.v2f(lit)):
        print("   ldr at 0x%08x: %s %s" % (a, m, o))
