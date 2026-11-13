import struct,sys,numpy as np
raw=open(sys.argv[1],'rb').read()
base=int(sys.argv[2],16)
def rec(o):
    i,r,f,w,h=struct.unpack_from('<HHIII',raw,o)
    return i,r,f,w&0xffff,(f>>16)&0xffff if False else h&0xffff
FPS=set([24,25,30,50,60,100,120,15,6,23,29,59,119,180,240,480,960,1000,2000,40,80,12,8,4,2,1,200,300,400,600])
SZ={(1920,1080),(1280,720),(720,480),(720,576),(3840,2160),(4096,2160),(6048,3400),(8652,4896),(3328,1872),(2496,1408),(1920,1200),(2560,1440),(3008,1696),(4160,2432),(8192,4320),(7952,4472),(4096,2304),(6048,4032),(3384,2256),(2256,3384),(4544,2552),(1504,1000)}
o=base
print(f'{"off":>9} {"id":>4} {"rsv":>5} {"fps":>6} {"w":>6} {"h":>6}')
while True:
    i,r,f,h=struct.unpack_from('<HHI',raw,o)
    w,hh=struct.unpack_from('<HH',raw,o+8)
    if not (0<=i<0x200 and r in (0,) and w<10000 and hh<10000): break
    print(f'{o:#09x} {i:4d} {r:5d} {f:6d} {w:6d} {hh:6d}')
    o+=12
    if o-base>0x1000: break
