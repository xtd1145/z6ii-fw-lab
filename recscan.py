import numpy as np,sys
path='all_modules/Z6_2_0170/eg1850_mas_01700'
raw=open(path,'rb').read()
U=np.frombuffer(raw,dtype='<u2',count=len(raw)//2).astype(np.int32)
SZ={3840,1920,1080,2160,1280,720,4096,2688,1200,3328,4160,2760,3384,2256,6048,4032,3016,2016,1024,768,540,576,480,424,8652,5568,4160,3024,4016,3008,2544,1696,2000}
FPSV={24,25,30,50,60,100,120,15,12,8,6,5000,6000,3000,2400,2500,12000,10000,5994,2997,2398,11988}
issz=np.isin(U,list(SZ)); isfps=np.isin(U,list(FPSV))
for S in (8,12,16,20,24,28,32,40,48,60):
    n=S//2
    if n*2!=S: continue
    nb=len(U)//n
    trimmed=U[:nb*n]
    ssz=issz[:nb*n].reshape(nb,n).sum(1)
    sfps=isfps[:nb*n].reshape(nb,n).sum(1)
    good=(ssz>=1)&(sfps>=1)
    # runs of good records
    best=[];cur=[]
    for i,g in enumerate(good):
        if g: cur.append(i)
        else:
            if len(cur)>=6: best.append((cur[0],cur[-1]))
            cur=[]
    if len(cur)>=6: best.append((cur[0],cur[-1]))
    for a,b in best:
        print(f'S={S:#4x} records {a}..{b} n={b-a+1} off {a*S:#09x}..{(b+1)*S:#09x}')
