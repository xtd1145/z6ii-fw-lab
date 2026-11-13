import re,sys,numpy as np
raw=open(sys.argv[1] if len(sys.argv)>1 else 'all_modules/Z6_2_0170/eg1850_mas_01700','rb').read()
pairs={'3840x2160':(b'\x00\x0f',b'\x70\x08'),'1920x1080':(b'\x80\x07',b'\x38\x04'),
       '1920x1200':(b'\x80\x07',b'\xb0\x04'),'2688x1512':(b'\x80\x0a',b'\xd8\x05'),
       '1280x720':(b'\x00\x05',b'\xd0\x02'),'4096x2160':(b'\x00\x10',b'\x70\x08'),
       '3840x1920':(b'\x00\x0f',b'\x80\x07'),'1920x2160':(b'\x80\x07',b'\x70\x08')}
for name,(a,b) in pairs.items():
    hits=[]
    for m in re.finditer(re.escape(a),raw):
        o=m.start()
        for d in (2,4,6,8,12,16,-2,-4,-6,-8):
            if raw[o+d:o+d+2]==b: hits.append(o)
    if hits:
        print(f'== {name}: {len(hits)}')
        for h in sorted(set(hits))[:40]:
            print(f'   {h:#09x} vma {h+0x10000000:#x}')
