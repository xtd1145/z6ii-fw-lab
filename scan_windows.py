import hashlib, mmap, os, sys, time
from multiprocessing import Pool

BODY_PATH = 'Z6_2_0170.dec'
WANT = bytes.fromhex('89019167cb702344a1afa1d44e408f26b259d3e4')
MODS = [
 ('all_modules/Z6_2_0170/eg1850_mas_01700', 0, 0x24f47ba),
 ('all_modules/Z6_2_0170/eg1850_sla_01700', 0, 0x86c4c2),
 ('all_modules/Z6_2_0170/ex1850_012000.bi', 0, 0x80002),
 ('all_modules/Z6_2_0170/vr1850_010100.bi', 0, 0xccc08),
 ('all_modules/Z6_2_0170/_tpj01_v140.bin', 0, 0xe002),
]

def job(args):
    path, start, end, tag = args
    t0 = time.time()
    body = open(BODY_PATH,'rb')
    body.seek(0x20)
    h0 = hashlib.sha1()
    while True:
        chunk = body.read(1 << 20)
        if not chunk: break
        h0.update(chunk)
    body.close()
    f = open(path,'rb')
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    hits = []
    n = 0
    for i in range(start, end, 1):
        h = h0.copy()
        h.update(mm[i:i+8])
        if h.digest() == WANT:
            hits.append(i)
            print('HIT', tag, hex(i), mm[i:i+8].hex(), flush=True)
    mm.close(); f.close()
    print('done', tag, hex(start), hex(end), n, '%.1fs' % (time.time()-t0), 'hits', hits, flush=True)
    return hits

if __name__ == '__main__':
    tasks = []
    CHUNK = 2_000_000
    for path, a, b in MODS:
        tag = os.path.basename(path)
        for s in range(a, b, CHUNK):
            tasks.append((path, s, min(s+CHUNK, b-7), tag))
    print('tasks', len(tasks), flush=True)
    t0 = time.time()
    with Pool(16) as p:
        res = p.map(job, tasks)
    print('ALL DONE %.1fs' % (time.time()-t0), [h for r in res for h in r])
