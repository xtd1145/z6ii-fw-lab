import numpy as np, sys
P=sys.argv[1] if len(sys.argv)>1 else 'all_modules/Z6_2_0170/eg1850_mas_01700'
D=open(P,'rb').read()
A=np.frombuffer(D,dtype='<u2')
av=A.astype(np.int64)
fps=set([2400,2398,2500,3000,2997,5000,6000,5994,10000,12000,11988,8000,4000,2000,1500,1080,960,720,360])
res=set([3840,1920,1080,2160,1280,720,640,424,2688,4096,3328,2496,2080,4160,5568,8256,8652,6048,4032,3024,795,530,1040,1536,2560])
fmask=np.isin(av,sorted(fps)); rmask=np.isin(av,sorted(res))
W=64
cf=np.convolve(fmask.astype(np.int32),np.ones(W,int),'same')
cr=np.convolve(rmask.astype(np.int32),np.ones(W,int),'same')
score=cf+cr*2
idx=np.where(score>=10)[0]
regs=[]
if len(idx):
    s=idx[0]; p=idx[0]
    for i in idx[1:]:
        if i-p>32: regs.append((s,p)); s=i
        p=i
    regs.append((s,p))
print("regions:",len(regs))
for a,b in regs:
    ao=a*2; bo=(b+1)*2
    print(f'--- off {ao:#x}-{bo:#x} vma {0x10000000+ao:#x} len {bo-ao:#x} fps={cf[a]} res={cr[a]}')
    seg=D[ao:min(bo,ao+160)]
    u=np.frombuffer(seg[:len(seg)//2*2],dtype='<u2')
    print('   ', ' '.join(f'{x:04x}' for x in u))
