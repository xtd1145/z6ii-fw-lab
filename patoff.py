import sys,re
p=sys.argv[1]; pat=bytes.fromhex(sys.argv[2])
raw=open(p,'rb').read()
for m in re.finditer(re.escape(pat),raw):
    print('0x%08x'%m.start())
