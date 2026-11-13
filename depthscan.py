import struct
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi'}
FS={24,25,30,50,60,100,120}
D={8,10,12,14,16}
for nm,p in files.items():
    raw=open(p,'rb').read(); m=len(raw)//2
    u=struct.unpack_from('<%dH'%m,raw,0)
    out=[]
    i=0
    while i<m-1:
        if u[i] in FS and u[i+1] in D:
            j=i;rows=[]
            while j+1<m and u[j] in FS and u[j+1] in D:
                rows.append((u[j],u[j+1]));j+=2
            if len(rows)>=5: out.append(('f,d',i*2,rows))
            i=j
        elif u[i] in D and u[i+1] in FS:
            j=i;rows=[]
            while j+1<m and u[j] in D and u[j+1] in FS:
                rows.append((u[j],u[j+1]));j+=2
            if len(rows)>=5: out.append(('d,f',i*2,rows))
            i=j
        else: i+=1
    # triples (fps,depth,?) via u32? and byte pairs
    print('==',nm,'hits',len(out))
    for o in out[:20]: print('   ',o[0],'0x%08x'%o[1],o[2][:20])
