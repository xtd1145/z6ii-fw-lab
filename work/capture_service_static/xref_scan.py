"""Read-only xref: find literal-pool words equal to a target VMA, verify the LDR that feeds them.

Usage:
  python xref_scan.py mas 0x1244c23c
  python xref_scan.py mas --str CaptureService
"""
import sys, struct, re, json, os
import fw

def litpool_hits(name, value, pool_only=True):
    d = fw.img(name)
    pat = struct.pack('<I', value)
    hits = []
    start = 0
    while True:
        i = d.find(pat, start)
        if i < 0:
            break
        if i % 4 == 0:
            hits.append(i)
        start = i + 1
    return hits


def find_loader(name, litoff, max_back=0x1000):
    """Scan backwards for a Thumb PC-relative LDR whose target is litoff."""
    lit_vma = fw.BASE + litoff
    out = []
    lo = max(0, litoff - max_back)
    # disassemble small windows preceding the literal; try every halfword start
    for back in range(4, max_back, 2):
        a = lit_vma - back
        if a < fw.BASE:
            break
        for ins in fw.dis_thumb(name, a, 4):
            if ins.id != 92:  # ldr.w
                pass
            if len(ins.operands) < 2:
                continue
            mem = ins.operands[-1]
            if mem.type != 3 or mem.mem.base != 11:
                continue
            lit = ((ins.address + 4) & ~3) + mem.mem.disp
            if lit == lit_vma:
                out.append((ins.address, ins.op_str))
                break
    return out


if __name__ == '__main__':
    name = sys.argv[1]
    tgt = sys.argv[2]
    v = int(tgt, 16)
    hits = litpool_hits(name, v)
    print(f"literal words == {v:08x}: {len(hits)}")
    for h in hits:
        loaders = find_loader(name, h)
        print(f"  pool@{h:08x} (vma {fw.BASE+h:08x})  loaders={[hex(x[0]) for x in loaders]}")
