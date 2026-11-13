import sys,struct
files={'Z6II-mas':'all_modules/Z6_2_0170/eg1850_mas_01700',
 'Z6II-sla':'all_modules/Z6_2_0170/eg1850_sla_01700',
 'Z8-eg':'ref_fw/all_modules/Z_8_0311/eg2070_0311b0.bi'}
FS={24,25,30,50,60,100,120}
def scan(name,raw):
    n=len(raw)//2
    v=struct.unpack_from('<%dH'%n,raw,0)
    out=[]
    # order (f,c)
    i=0
    while i<n-1:
        if v[i] in FS and v[i+1]<=3:
            j=i;cnt=0;fs=[];cs=[]
            while j+1<n and v[j] in FS and v[j+1]<=3:
                fs.append(v[j]);cs.append(v[j+1]);j+=2;cnt+=1
            if cnt>=8 and len(set(cs))>=2 and len(set(fs))>=3:
                out.append(('H(f,c)',i*2,cnt,cs,fs))
            i=j
        else: i+=1
    # byte triples (c, s<=7, f)
    b=raw
    i=0;N=len(b)-3
    while i<N:
        if b[i]<=3 and b[i+1]<=7 and b[i+2] in FS:
            j=i;cnt=0;cs=[];ss=[];fs=[]
            while j+2<N and b[j]<=3 and b[j+1]<=7 and b[j+2] in FS:
                cs.append(b[j]);ss.append(b[j+1]);fs.append(b[j+2]);j+=3;cnt+=1
            if cnt>=10 and len(set(cs))>=2:
                out.append(('B(c,s,f)',i,cnt,list(zip(cs,ss,fs))))
            i=j
        else: i+=1
    # byte pairs (c,f) stride2 with f as byte
    i=0
    while i<N:
        if b[i]<=3 and b[i+1] in FS:
            j=i;cnt=0;cs=[];fs=[]
            while j+1<N and b[j]<=3 and b[j+1] in FS:
                cs.append(b[j]);fs.append(b[j+1]);j+=2;cnt+=1
            if cnt>=10 and len(set(cs))>=2 and len(set(fs))>=3:
                out.append(('B(c,f)',i,cnt,fs))
            i=j
        else: i+=1
    print('==',name,len(out))
    for o in out[:40]: print('  ',o[0],'0x%08x'%o[1],'n=%d'%o[2],o[3][:60])
for nm,p in files.items():
    scan(nm,open(p,'rb').read())
