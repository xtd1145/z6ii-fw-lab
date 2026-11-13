import sys
from capstone import *
from capstone.arm import *

import os
PATH = os.environ.get('MOD','all_modules/Z6_2_0170/eg1850_mas_01700')
D = open(PATH,'rb').read()
BASE = 0x10000000

def dis(off, n, thumb=True):
    code = D[off:off+n]
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
    md.detail = True
    md.skipdata = True
    out=[]
    for i in md.disasm(code, BASE+off):
        line=f"{i.address:08x}: {i.mnemonic:<8} {i.op_str}"
        anns=[]
        try:
            ops=list(i.operands)
        except Exception:
            ops=[]
        for op in ops:
            if op.type == ARM_OP_MEM and op.mem.base == ARM_REG_PC:
                a = ((i.address + 4) & ~3) + op.mem.disp
                if BASE <= a < BASE+len(D):
                    v = int.from_bytes(D[a-BASE:a-BASE+4],'little')
                    b = D[a-BASE:a-BASE+8]
                    s = ''.join(chr(c) if 32<=c<127 else '.' for c in b)
                    anns.append(f"@{a:08x}={v:08x} '{s}'")
                    if BASE <= v < BASE+len(D):
                        v2 = int.from_bytes(D[v-BASE:v-BASE+4],'little')
                        anns.append(f"->{v2:08x}")
            if i.mnemonic == 'adr':
                a = (i.address + 4) & ~3
                try:
                    imm = int(i.op_str.split('#')[1],0)
                    tgt = a + imm if imm < 0x400 else a + imm
                    if tgt < 0x400: tgt = a + imm
                    # capstone reports imm for T1; target = align(pc,4)+imm
                    s2 = ''.join(chr(c) if 32<=c<127 else '.' for c in D[tgt-BASE:tgt-BASE+16])
                    anns.append(f"adr->{tgt:08x} '{s2}'")
                except Exception: pass
        if anns: line += "   ; " + "  ".join(anns)
        out.append(line)
    print('\n'.join(out))

if __name__=='__main__':
    off=int(sys.argv[1],0); n=int(sys.argv[2],0)
    thumb = (len(sys.argv)<4 or sys.argv[3]!='arm')
    dis(off,n,thumb)
