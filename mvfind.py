import struct,sys,numpy as np
FPS=set([23,24,25,30,40,48,50,60,75,90,100,120,150,180,200,240,300,480,600,15,12,8,6,4,2,1,1000,500])
def scan(path):
    raw=open(path,'rb').read()
    ok=[]
    for o in range(0,len(raw)-12,4):
        i,r,f=struct.unpack_from('<HHI',raw,o)
        w,h=struct.unpack_from('<HH',raw,o+8)
        if r==0 and f in FPS and 0<w<=9000 and 0<h<=9000 and (w,w) not in []:
            ok.append(o)
    # cluster runs with stride 12
    runs=[]
    for o in ok:
        if runs and o-runs[-1][-1]==12: runs[-1].append(o)
        else: runs.append([o])
    return raw,[r for r in runs if len(r)>=6]
for p in sys.argv[1:]:
    raw,runs=scan(p)
    print(f'### {p}  runs={len(runs)}')
    for r in runs:
        print(f'  run {r[0]:#09x}..{r[-1]+12:#09x}  n={len(r)}')
