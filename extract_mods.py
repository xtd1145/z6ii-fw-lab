import struct,sys,os
def mods(path):
    raw=open(path,'rb').read()
    body=raw[0x20:]
    assert body[:32]==b' '*32, body[:32]
    n=struct.unpack_from('>I',body,0x20)[0]
    out=[]
    for i in range(n):
        e=body[0x30+32*i:0x30+32*(i+1)]
        name=e[:16].rstrip(b'\0').decode('latin1')
        off,ln=struct.unpack_from('>II',e,16)
        out.append((name,off,ln))
    return body,out
p=sys.argv[1]; outdir=sys.argv[2]
body,ms=mods(p)
os.makedirs(outdir,exist_ok=True)
for name,off,ln in ms:
    print(f'{name:24s} off={off:#x} len={ln:#x}')
    open(os.path.join(outdir,name),'wb').write(body[off:off+ln])
