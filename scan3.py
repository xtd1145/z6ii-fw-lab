import numpy as np, sys
P='all_modules/Z6_2_0170/eg1850_mas_01700'
raw=open(P,'rb').read()
FPS=[2398,2400,2500,2997,3000,5000,5994,6000,10000,11988,12000]
u16=np.frombuffer(raw[:len(raw)//2*2],dtype='<u2').astype(np.int64)
lo=int(sys.argv[1],16); hi=int(sys.argv[2],16)
sub=u16[lo//2:hi//2]
m=np.isin(sub,FPS)
idx=np.where(m)[0]
print('hits',len(idx))
prev=None
runs=[]
for i in idx:
    if prev is None or i-prev>8: runs.append([i,i])
    else: runs[-1][1]=i
    prev=i
runs=[r for r in runs if r[1]-r[0]>=4]
for r in runs:
    a=lo+r[0]*2; b=lo+(r[1]+1)*2
    print(f'-- {a:#09x}..{b:#09x}')
    seg=np.frombuffer(raw[a-8:b+8],dtype='<u2')
    print('   ',' '.join(f'{x}' for x in seg))
