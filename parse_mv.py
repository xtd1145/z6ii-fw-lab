import re,sys,numpy as np
raw=open(sys.argv[1] if len(sys.argv)>1 else 'all_modules/Z6_2_0170/eg1850_mas_01700','rb').read()
pats=[b'\x00\x0f\x70\x08',b'\x80\x07\x38\x04',b'\x00\x05\xd0\x02',b'\x80\x0a\xd8\x05',b'\x00\x10\x70\x08',b'\x80\x07\xb0\x04',b'\x00\x04\x90\x01']
hits=[]
for p in pats:
    for m in re.finditer(re.escape(p),raw): hits.append(m.start())
hits=sorted(set(hits))
print('addr deltas:',[hex(b-a) for a,b in zip(hits,hits[1:])][:60])
def u16(o): return int(np.frombuffer(raw,dtype='<u2',count=1,offset=o)[0])
def u32(o): return int(np.frombuffer(raw,dtype='<u4',count=1,offset=o)[0])
for h in hits:
    print(f'{h:#09x} ' + ' '.join(f'{u32(h+i)}' for i in range(0,40,4)))
