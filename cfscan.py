import sys,struct
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi',
 'Z7II-mas':'ref_fw/all_modules/Z7_2_0170/eg1840_mas_01700'}
FS={24,25,30,50,60,100,120}
for nm,p in files.items():
    raw=open(p,'rb').read()
    n=len(raw)//2
    v=struct.unpack_from('<%dH'%n,raw,0)
    print('===',nm)
    i=0
    while i<n-1:
        if v[i]<=3 and v[i+1] in FS:
            j=i; cnt=0; cs=[]; fs=[]
            while j+1<n and v[j]<=3 and v[j+1] in FS:
                cs.append(v[j]); fs.append(v[j+1]); j+=2; cnt+=1
            if cnt>=8 and len(set(cs))>=2 and len(set(fs))>=3:
                print('  0x%08x n=%d c=%s f=%s'%(i*2,cnt,','.join(map(str,cs)),','.join(map(str,fs))))
            i=j
        else: i+=1
