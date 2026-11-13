import hashlib, struct
T=[]
for i in range(256):
    c=i<<8
    for _ in range(8):
        c=((c<<1)^0x1021)&0xFFFF if c&0x8000 else (c<<1)&0xFFFF
    T.append(c)
def crc16(b,init=0):
    c=init
    for x in b: c=((c<<8)&0xFFFF)^T[((c>>8)^x)&0xFF]
    return c
D=open('Z6_2_0170.dec','rb').read(); N=len(D)
hdr=D[:20]; body=D[0x20:]
cnt=struct.unpack('>I',body[0x20:0x24])[0]
dsz=struct.unpack('>I',body[0x24:0x28])[0]
print('modules',cnt,'dirsize',hex(dsz),'body',hex(len(body)))
mods=[]
for i in range(cnt):
    e=body[0x30+32*i:0x50+32*i]
    name=e[:16].rstrip(b'\0').decode()
    off=struct.unpack('>I',e[16:20])[0]; ln=struct.unpack('>I',e[20:24])[0]
    mods.append((name,off,ln))
    pay=body[off:off+ln-2]; stored=struct.unpack('>H',body[off+ln-2:off+ln])[0]
    print('  %-22s off=%#09x len=%#09x crc=%04x calc=%04x %s'%(name,off,ln,stored,crc16(pay),'OK' if crc16(pay)==stored else 'BAD'))
pkg_stored=struct.unpack('>H',body[-16:-14])[0]
pkg_calc=crc16(body[:-16])
print('package CRC stored=%04x calc=%04x %s'%(pkg_stored,pkg_calc,'OK' if pkg_stored==pkg_calc else 'BAD'))
SEED=bytes.fromhex('0023002400250026')
print('SHA1(body||0023002400250026) =',hashlib.sha1(body+SEED).hexdigest())
print('header digest               =',hdr.hex())
