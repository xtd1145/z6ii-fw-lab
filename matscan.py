import sys,struct
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi'}
for nm,p in files.items():
    raw=open(p,'rb').read()
    print('===',nm)
    runs=[];i=0;N=len(raw)
    while i<N:
        if raw[i]<=3:
            j=i+1
            while j<N and raw[j]<=3: j+=1
            seg=raw[i:j]
            if len(seg)>=40 and seg.count(1)>=3 and seg.count(0)>=3:
                runs.append((i,bytes(seg)))
            i=j
        else: i+=1
    print('  candidates',len(runs))
    for off,seg in runs:
        print('   0x%08x len=%3d %s'%(off,len(seg),' '.join(str(b) for b in seg)))
