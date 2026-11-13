import numpy as np, sys, collections
P=sys.argv[1]
raw=open(P,'rb').read()
BASE=0x10000000
u16=np.frombuffer(raw[:len(raw)//2*2],dtype='<u2').astype(np.int64)
u32=np.frombuffer(raw[:len(raw)//4*4],dtype='<u4').astype(np.int64)
FPS16=[2398,2400,2500,2997,3000,5000,5994,6000,10000,11988,12000,15000,20000,24000,40000,48000,60000]
RES=[3840,4096,3328,2688,2496,2080,1920,1280,1088,1080,720,640,544,512,424,240]
BD=[8,10,12,14,16]
f16=np.isin(u16,FPS16)
print('u16 fps-coded hit addresses:')
c=collections.Counter()
for i in np.where(f16)[0]:
    c[i*2//0x40000]+=1
for k,v in sorted(c.items(), key=lambda x:-x[1])[:40]:
    print(f'  chunk {k*0x40000:#09x} hits={v}')
