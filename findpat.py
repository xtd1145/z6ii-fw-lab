import sys,re
pat=re.escape(bytes.fromhex(sys.argv[1]))
for p in sys.argv[2:]:
    raw=open(p,'rb').read()
    hits=[m.start() for m in re.finditer(pat,raw)]
    print(f'{p}: {len(hits)} -> {[hex(h) for h in hits[:20]]}')
