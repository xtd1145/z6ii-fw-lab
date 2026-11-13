import sys,struct
p=sys.argv[1]; off=int(sys.argv[2],16); n=int(sys.argv[3])
raw=open(p,'rb').read()
vals=struct.unpack_from('<%dH'%n,raw,off)
for i in range(0,n,12):
    print('%08x  '%(off+i*2)+' '.join('%5d'%v for v in vals[i:i+12]))
