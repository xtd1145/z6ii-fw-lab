import sys, struct

path = 'all_modules/Z6_2_0170/eg1850_mas_01700'
b = open(path,'rb').read()
BASE = 0x10000000
targets = {
 0x1037ea40:'verify_sha1', 0x1037e886:'verify_crc_dispatch', 0x1037e768:'image_scan_dispatch',
 0x1037e63e:'entry_check', 0x1037e6dc:'sanity', 0x1038f6c6:'decrypt',
 0x10392286:'verify', 0x1039ea3c:'transform', 0x1039eabc:'transform2',
 0x10391142:'set_key', 0x10391120:'zero_ctx',
}
hits = {v:[] for v in targets.values()}
# Thumb BL scan, 4-byte aligned instruction stream is not guaranteed; scan every halfword
n = len(b)-4
for off in range(0, n, 2):
    hw1 = b[off] | (b[off+1]<<8)
    if (hw1 & 0xF800) != 0xF000:
        continue
    hw2 = b[off+2] | (b[off+3]<<8)
    if (hw2 & 0xD000) != 0xD000:
        continue
    S = (hw1>>10)&1
    imm10 = hw1 & 0x3FF
    J1 = (hw2>>13)&1
    J2 = (hw2>>11)&1
    imm11 = hw2 & 0x7FF
    I1 = 1 ^ (J1 ^ S)
    I2 = 1 ^ (J2 ^ S)
    imm = (S<<24)|(I1<<23)|(I2<<22)|(imm10<<12)|(imm11<<1)
    if S: imm -= (1<<25)
    pc = BASE + off + 4
    tgt = (pc + imm) & 0xFFFFFFFF
    if tgt in targets:
        hits[targets[tgt]].append(off)
for name,offs in hits.items():
    print(name, hex(targets and 0), len(offs), ' '.join(hex(o) for o in offs[:20]))
