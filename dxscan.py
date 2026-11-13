import sys,struct,re
paths={
 'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi',
}
# DX still widths / heights for Z6 II
W={4160:0x1040,4000:0x0fa0,3120:0x0c30,2080:0x0820,1664:0x0680}
H={2784:0x0ae0,2664:0x0a68,2088:0x0828,1392:0x0570,2352:0x0930,2336:0x0920,1104:0x0450}
for name,p in paths.items():
    raw=open(p,'rb').read()
    hits=[]
    for _,wv in W.items():
        b=struct.pack('<H',wv)
        for m in re.finditer(re.escape(b),raw):
            o=m.start()
            lo=o&~1
            for _,hv in H.items():
                hb=struct.pack('<H',hv)
                j=raw.find(hb,max(0,o-16),o+16)
                if j>=0:
                    hits.append((lo,wv,hv,j-lo))
    hits=sorted(set(hits))
    print('==',name,len(hits))
    # cluster
    prev=None
    for h in hits:
        if prev is None or h[0]-prev>0x40: print('---')
        print('  off=0x%08x %d x %d d=%d'%(h[0],h[1],h[2],h[3]))
        prev=h[0]
