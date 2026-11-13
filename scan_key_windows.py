import hashlib, sys, os, struct
from multiprocessing import Pool
D=open('Z6_2_0170.dec','rb').read()
HDR=D[:20]
def make_range(a,b):
    return hashlib.sha1(D[a:b]), a, b
base, A, B = make_range(0x20, len(D))
def work(args):
    lo,hi=args
    h0=base.copy()
    res=[]
    for i in range(lo,hi):
        h=h0.copy(); h.update(D[i:i+8])
        if h.digest()==HDR: res.append(i)
    return res
if __name__=='__main__':
    N=len(D)
    step=200000
    chunks=[(i,min(i+step,N-7)) for i in range(0,N-7,step)]
    with Pool(8) as p:
        out=[]
        for i,r in enumerate(p.imap_unordered(work,chunks)):
            if r: print('HIT',[hex(x) for x in r],flush=True); out+=r
        print('done hits',out,flush=True)
