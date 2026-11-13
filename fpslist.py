import sys,struct
p='all_modules/Z6_2_0170/eg1850_mas_01700'
raw=open(p,'rb').read()
S={24,25,30,50,60,100,120,1,2,3,5}
FS={24,25,30,50,60,100,120}
n=len(raw)//2
v=struct.unpack_from('<%dH'%n,raw,0)
runs=[]
i=0
while i<n:
    if v[i] in FS:
        j=i+1
        while j<n and v[j] in FS and v[j]!=v[j-1]:
            j+=1
        if j-i>=4: runs.append((i,j-i))
        i=j
    else: i+=1
print('runs',len(runs))
# cluster close runs
prev=-1
for idx,ln in runs:
    off=idx*2
    if prev>=0 and off-prev<0x200: print('  (cont)')
    print('== off=0x%08x len=%d  %s'%(off,ln,' '.join(str(x) for x in v[idx:idx+ln])))
    print('   ctx:',' '.join(str(x) for x in v[max(0,idx-6):idx+ln+6]))
    prev=off+ln*2
