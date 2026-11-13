import sys,struct
def u16s(p,off,n): return struct.unpack_from('<%dH'%n,open(p,'rb').read(),off)
A='all_modules/Z6_2_0170/eg1850_mas_01700'; ao=0x19dae9c
B='ref_fw/all_modules/Z7_2_0170/eg1840_mas_01700'; bo=0x19ca55c
n=512
a=u16s(A,ao,n); b=u16s(B,bo,n)
for i in range(0,n,8):
    ma=' '.join('%6d'%x for x in a[i:i+8]); mb=' '.join('%6d'%x for x in b[i:i+8])
    d='  ' if a[i:i+8]==b[i:i+8] else 'XX'
    print('%s %08x/%08x  %-50s | %-50s'%(d,ao+i*2,bo+i*2,ma,mb))
