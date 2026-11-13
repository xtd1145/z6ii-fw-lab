"""xref_addr.py -- who loads a literal whose *address* falls in a VMA window?

Scans Thumb-2 code for LDR-literal (16-bit 0x48xx and 32-bit 0xF8DF) and reports
the ones whose literal slot address lies inside [lo,hi). This finds the consumer
of a rodata table without needing full disassembly.

usage: python xref_addr.py <module> <lo_vma> <hi_vma>
"""
import struct, sys
import fw


def scan(name, lo, hi, limit=200):
    b = fw.load(name)
    n = len(b)
    out = []
    for a in range(0, n - 4, 2):
        hw = b[a] | (b[a + 1] << 8)
        tgt = None
        if (hw & 0xF800) == 0x4800:
            rt = hw & 7
            imm = (hw & 0xFF) * 4
            tgt = ((a + 4) & ~3) + imm
            kind = "ldr16"
        elif hw == 0xF8DF:
            hw2 = b[a + 2] | (b[a + 3] << 8)
            rt = (hw2 >> 12) & 0xF
            imm = hw2 & 0xFFF
            tgt = ((a + 4) & ~3) + imm
            kind = "ldr32"
        else:
            continue
        v = fw.f2v(tgt)
        if lo <= v < hi:
            out.append((a, kind, rt, v, struct.unpack_from("<I", b, tgt)[0]))
            if len(out) >= limit:
                break
    return out


if __name__ == "__main__":
    name = sys.argv[1]
    lo = int(sys.argv[2], 0)
    hi = int(sys.argv[3], 0)
    print("== %s: LDR literals whose slot is in [%08x,%08x)" % (name, lo, hi))
    for a, kind, rt, v, word in scan(name, lo, hi):
        print("   insn %08x %-6s r%-2d slot %08x  word %08x" % (fw.f2v(a), kind, rt, v, word))
