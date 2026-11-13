import struct,sys
raw=open(sys.argv[1],'rb').read()
for start in [int(x,16) for x in sys.argv[2:]]:
    n=int(sys.argv[-1]) if sys.argv[-1].isdigit() else 80
    print(f'== {sys.argv[1]} @{start:#x}')
    o=start
    for k in range(80):
        i,r,f=struct.unpack_from('<HHI',raw,o)
        w,h=struct.unpack_from('<HH',raw,o+8)
        print(f'  {o:#09x} id={i:4d} rsv={r:5d} fps={f:6d} {w}x{h}')
        o+=12
