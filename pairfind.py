import sys,struct
pats={
 '4160x2784':(4160,2784),'4160x2352':(4160,2352),'4160x2336':(4160,2336),
 '3120x2088':(3120,2088),'2080x1392':(2080,1392),'4000x2664':(4000,2664),
 '6048x4024':(6048,4024),'3840x2160':(3840,2160),'4096x2160':(4096,2160),
 '5568x3712':(5568,3712),'4032x2268':(4032,2268),'4160x2784b':(4160,2784),
}
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi',
 'Z7II-mas':'ref_fw/all_modules/Z7_2_0170/eg1840_mas_01700'}
for nm,p in files.items():
    raw=open(p,'rb').read()
    print('===',nm,len(raw))
    for k,(w,h) in pats.items():
        b=struct.pack('<HH',w,h)
        idx=[]; i=raw.find(b)
        while i>=0:
            if i%2==0: idx.append(i)
            i=raw.find(b,i+1)
        if idx: print('  %-12s %s'%(k,' '.join('0x%x'%x for x in idx[:24]), ), '(n=%d)'%len(idx))
