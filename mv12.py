import sys,struct
p,o0,o1=sys.argv[1],int(sys.argv[2],16),int(sys.argv[3],16)
raw=open(p,'rb').read()
FPS={23.976:24,24:24,25:25,29.97:30,30:30,48:48,50:50,59.94:60,60:60,100:100,119.88:120,120:120}
for o in range(o0,o1,12):
    i,z=struct.unpack_from('<HH',raw,o)
    f,=struct.unpack_from('<I',raw,o+4)
    w,h=struct.unpack_from('<HH',raw,o+8)
    print('  0x%08x id=%-4d pad=%-5d fps=%-6d %5dx%-5d  raw=%s'%(o,i,z,f,w,h,raw[o:o+12].hex()))
