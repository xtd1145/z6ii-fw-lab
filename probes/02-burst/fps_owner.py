import struct, re, collections

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
raw = open(MAS, "rb").read()

print("== A: u16 grid around the still release-rate ladder 0x92F648")
base = 0x92F600
for off in range(base, base + 0x140, 0x10):
    row = [struct.unpack_from("<8H", raw, off)]
    txt = "".join(f"{v:6d}" if v < 4000 else f"{'-':>6}" for v in row[0])
    mark = "  <-- ladder" if off <= 0x92F648 < off + 0x10 else ""
    print("    0x%06x %s%s" % (off, txt, mark))

print("\n== B: does any u16/u8 record pair a still size id (8/9/10) with 30 or 45?")
# still size records were found at 0x9417B4 as {w,h,id} triples
for i in range(0, 20):
    o = 0x9417B4 + i * 12
    w, h, k = struct.unpack_from("<HHI", raw, o)
    if w == 0:
        break
    print("    0x%06x  %5d x %-5d id=%d" % (o, w, h, k))

print("\n== C: names mentioning frame rate / release / limit in mas")
pat = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{3,40}")
cand = collections.Counter()
for m in pat.finditer(raw):
    s = m.group().decode()
    if re.search(r"(?i)(fps|framerate|frametimer|releasemax|maxdrive|drivespeed|"
                 r"shutterspeed|ch_cl|climit|ratecap|maxrate)", s):
        cand[s] += 1
for s, n in sorted(cand.items())[:60]:
    print("    %-42s %d" % (s, n))

print("\n== D: strings around 'ReleaseMode' / CH / CL still-quality tokens")
for kw in (b"ReleaseMode", b"RELEASE_MODE", b"CHQuiet", b"Quiet", b"BurstRate",
           b"MaxFps", b"FPS_", b"_FPS", b"DriveMode"):
    hits = [m.start() for m in re.finditer(re.escape(kw), raw)]
    if hits:
        print("    %-14s %4d hits  first %s" % (kw.decode(), len(hits),
                                                ["0x%x" % h for h in hits[:4]]))
