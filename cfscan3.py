import struct
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi',
 'Z7II-mas':'ref_fw/all_modules/Z7_2_0170/eg1840_mas_01700'}
FS={24,25,30,50,60,100,120,15,16,23.98 and 24}
def sc(name,raw):
    n=len(raw)//4
    v=struct.unpack_from('<%dI'%n,raw,0)
    res=[]
    # pattern (fps, 0, crop, 0) or (crop,0,fps,0) as u32 pairs with high u16 zero
    for i in range(n-1):
        a,b=v[i],v[i+1]
        if a in FS and b<=3:
            j=i;cnt=0;fs=[];cs=[]
            while j+1<n and v[j] in FS and v[j+1]<=3 and v[j]<0x10000:
                fs.append(v[j]);cs.append(v[j+1]);j+=1;cnt+=1
            if cnt>=5 and len(set(cs))>=2 and len(set(fs))>=3:
                res.append(('F,C',i*4,cnt,list(zip(fs,cs))))
    # triples u16 (c,s,f) stride 6
    m=len(raw)//2
    u=struct.unpack_from('<%dH'%m,raw,0)
    i=0
    while i<m-2:
        if u[i]<=3 and u[i+1]<=8 and u[i+2] in FS:
            j=i;cnt=0;rows=[]
            while j+2<m and u[j]<=3 and u[j+1]<=8 and u[j+2] in FS:
                rows.append((u[j],u[j+1],u[j+2]));j+=3;cnt+=1
            if cnt>=6 and len(set(r[0] for r in rows))>=2:
                res.append(('C,S,F',i*2,cnt,rows))
            i=j
        else: i+=1
    print('==',name,len(res))
    for r in res[:30]: print('  ',r[0],'0x%08x'%r[1],r[2],r[3][:40])
for nm,p in files.items(): sc(nm,open(p,'rb').read())
