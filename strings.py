import re,sys
D=open(sys.argv[1],'rb').read()
pat=re.compile(rb'[\x20-\x7e]{4,}')
seen={}
for m in pat.finditer(D):
    s=m.group().decode()
    if len(sys.argv)>2 and not re.search(sys.argv[2],s,re.I): continue
    seen.setdefault(s,[]).append(m.start())
for s,offs in sorted(seen.items(), key=lambda kv:kv[1][0]):
    print(f'{offs[0]:#09x} x{len(offs)} {s[:120]}')
