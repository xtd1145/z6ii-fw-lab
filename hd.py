import sys,struct
# usage: hd.py FILE OFF LEN [stride]
p=sys.argv[1]; off=int(sys.argv[2],16); ln=int(sys.argv[3],16)
st=int(sys.argv[4],16) if len(sys.argv)>4 else 8
raw=open(p,'rb').read()
d=raw[off:off+ln]
for i in range(0,len(d),st):
    row=d[i:i+st]
    ws=[]
    for j in range(0,len(row),4):
        if len(row[j:j+4])==4: ws.append('%08x'%struct.unpack_from('<I',row,j)[0])
        else: ws.append(row[j:].hex()+'..')
    print('%08x  %-40s  %s'%(off+i,' '.join(ws),''.join(chr(c) if 32<=c<127 else '.' for c in row)))
