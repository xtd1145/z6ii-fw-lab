import re,sys,numpy as np
path=sys.argv[1] if len(sys.argv)>1 else 'all_modules/Z6_2_0170/eg1850_mas_01700'
raw=open(path,'rb').read()
def le(v): return np.uint16(v).tobytes() if v<0x10000 else np.uint32(v).tobytes()
cands={'6048x4032':(6048,4032),'3384x2256':(3384,2256),'4160x2760':(4160,2760),'5568x3712':(5568,3712),
       '6000x4000':(6000,4000),'3000x2000':(3000,2000),'8652x2048':(8652,2048),'4544x2556':(4544,2556),
       '7952x5304':(7952,5304),'6048x3376':(6048,3376),'6048x3408':(6048,3408),'4032x6048':(4032,6048)}
for k,(w,h) in cands.items():
    pw=np.uint32(w).tobytes(); ph=np.uint32(h).tobytes()
    u1w=np.uint16(w).tobytes(); u1h=np.uint16(h).tobytes()
    out=[]
    for m in re.finditer(re.escape(u1w),raw):
        o=m.start()
        for d in (2,4,6):
            if raw[o+d:o+d+2]==u1h: out.append(o)
    for m in re.finditer(re.escape(np.uint32(w).tobytes()),raw):
        o=m.start()
        for d in (4,8):
            if raw[o+d:o+d+4]==ph: out.append(o)
    if out:
        print(f'== {k}: {sorted(set(out))[:40]}')
