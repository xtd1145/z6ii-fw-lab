import os, glob, binascii, re

MODS = {
 'mas': 'all_modules/Z6_2_0170/eg1850_mas_01700',
 'sla': 'all_modules/Z6_2_0170/eg1850_sla_01700',
 'ex':  'all_modules/Z6_2_0170/ex1850_012000.bi',
 'vr':  'all_modules/Z6_2_0170/vr1850_010100.bi',
 'tpj': 'all_modules/Z6_2_0170/_tpj01_v140.bin',
}

def hexdump(b, base=0, n=0x100):
    out=[]
    for off in range(0, min(n, len(b)), 16):
        chunk=b[off:off+16]
        h=' '.join(f'{x:02x}' for x in chunk)
        a=''.join(chr(x) if 32<=x<127 else '.' for x in chunk)
        out.append(f'{base+off:08x}  {h:<47}  {a}')
    return '\n'.join(out)

for name,path in MODS.items():
    b=open(path,'rb').read()
    print('='*80)
    print(name, path, 'len', hex(len(b)), f'({len(b)})')
    print(hexdump(b, 0, 0x120))
    # pattern searches
    pats = {
      'stub_c0': bytes.fromhex('0023002400250026'),
      'stub_c1': bytes.fromhex('0024002500260027'),
      'seedptr': bytes.fromhex('88000010'),
      'sha1iv': bytes.fromhex('0123456789abcdef'),
      'Z_6_2': b'Z_6_2', 'Z6_2': b'Z6_2', 'Z_7_2': b'Z_7_2', 'Z_5_0': b'Z_5_0',
      'Z_8': b'Z_8', 'FCL': b'FirmwareChkLib',
    }
    for pn,pb in pats.items():
        hits=[]
        start=0
        while True:
            i=b.find(pb,start)
            if i<0: break
            hits.append(i)
            start=i+1
            if len(hits)>=12: break
        print(f'  pattern {pn} ({pb.hex()}) hits: ' + (', '.join(hex(h) for h in hits) if hits else 'none'))
        if pn in ('stub_c0','stub_c1','seedptr','Z_6_2','Z_7_2','Z_5_0') and hits:
            for h in hits[:6]:
                print(hexdump(b[h-16 if h>=16 else 0:h+32], (h-16 if h>=16 else 0), 48))
