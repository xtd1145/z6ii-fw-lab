import numpy as np, sys
raw=open(sys.argv[1],'rb').read()
D=np.frombuffer(raw,dtype=np.uint8)
BASE=int(sys.argv[2],0) if len(sys.argv)>2 else 0x10000000
A=D[:len(raw)//4*4].view(np.uint32).astype(np.int64)
CH=0x20000
out=[]
for i in range(0,len(A)*4,CH):
    a=A[i//4:(i+CH)//4]
    if len(a)==0: break
    pr=((a>=BASE)&(a<BASE+len(raw))).mean()
    zr=(a==0).mean()
    cls='DATA' if pr>0.05 or zr>0.5 else 'code'
    out.append((i,pr,zr,cls))
for i,pr,zr,cls in out: print(f'{i:#09x} {pr:6.3f} {zr:6.3f} {cls}')
# print boundaries
print('--- transitions')
for j in range(1,len(out)):
    if out[j][3]!=out[j-1][3]: print(f'  {out[j][0]:#09x} -> {out[j][3]}')
