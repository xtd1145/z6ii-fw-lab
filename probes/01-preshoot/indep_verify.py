import os, re

ROOT = r"D:\z6ii_fw_lab\all_modules"
mods = {}
for d in sorted(os.listdir(ROOT)):
    for f in sorted(os.listdir(os.path.join(ROOT, d))):
        mods[(d, f)] = open(os.path.join(ROOT, d, f), "rb").read()
        print("loaded %s/%s  len=0x%x" % (d, f, len(mods[(d, f)])))

def find_all(pat):
    out = []
    for (d, f), b in mods.items():
        for m in re.finditer(re.escape(pat), b):
            out.append((d, f, m.start()))
    return out

print("\n== 1. 'Pre-release' as UTF-16LE and as ASCII")
for label, pat in (("utf16", "Pre-release".encode("utf-16-le")),
                   ("ascii", b"Pre-release")):
    h = find_all(pat)
    by = {}
    for d, f, o in h:
        by.setdefault((d, f), []).append(o)
    print("   %s: total %d" % (label, len(h)))
    for k, v in sorted(by.items()):
        print("      %-14s %-22s %2d  %s" % (k[0], k[1], len(v),
                                             ["0x%x" % x for x in v[:8]]))

print("\n== 2. C60 / C30 / C120 tokens (utf16 exact + ascii with word boundary)")
def find_re(rx):
    out = []
    for (d, f), b in mods.items():
        for m in rx.finditer(b):
            out.append((d, f, m.start()))
    return out

for name in ("C60", "C30", "C120"):
    p16 = name.encode("utf-16-le")
    h16 = find_all(p16)
    asc = find_re(re.compile(rb"(?<![0-9A-Za-z])" + name.encode() + rb"(?![0-9])"))
    per16, perasc = {}, {}
    for d, f, o in h16:
        per16[(d, f)] = per16.get((d, f), 0) + 1
    for d, f, o in asc:
        perasc[(d, f)] = perasc.get((d, f), 0) + 1
    print("   %-5s utf16: %s" % (name, {("%s/%s" % k): v for k, v in
                                        sorted(per16.items()) if v >= 2}))
    print("   %-5s ascii: %s" % (name, {("%s/%s" % k): v for k, v in
                                        sorted(perasc.items()) if v >= 3}))

print("\n== 3. read the claimed Z8 anchor 0x031568e0 and its neighbours")
for (d, f), b in mods.items():
    if d != "Z_8_0311":
        continue
    for off, what in ((0x031568e0, "Pre-release burst"), (0x031568da, "C60"),
                      (0x0315690a, "Post-release burst"),
                      (0x03156936, "High fps viewfinder"),
                      (0x033a489c, "title variant"), (0x0242e730, "CAPSVC"),
                      (0x04cce4f1, "HSFCPROC"), (0x04cceabd, "CAPLV"),
                      (0x04cce4d5, "SHTSND"), (0x04ce5006, "CSQDFL region"),
                      (0x0295b470, "Z8 cmd pool"), (0x03161000, "help text")):
        if off < len(b):
            win = b[off:off + 0x60]
            u16 = win.decode("utf-16-le", errors="replace")[:48]
            asc = re.sub(rb"[^\x20-\x7e]", b".", win).decode()[:48]
            print("   %-22s %s/0x%08x  u16=%r  ascii=%r" % (what, f[:14], off,
                                                            u16[:40], asc))

print("\n== 4. CSQ* tokens per module")
for (d, f), b in sorted(mods.items()):
    n = len(re.findall(rb"CSQ[A-Z0-9]{2,6}", b))
    n16 = len(re.findall("CSQ".encode("utf-16-le"), b))
    if n or n16:
        print("   %-14s %-22s ascii=%-5d utf16prefix=%d" % (d, f, n, n16))
print("   (modules not listed have zero CSQ hits)")

print("\n== 5. Z6II claimed call-site 0x10379f64 (as vma -> file, and as raw file off)")
for (d, f), b in sorted(mods.items()):
    if "Z6_2" not in d:
        continue
    for lbl, off in (("vma-0x10000000", 0x10379F64 - 0x10000000), ("raw", 0x10379F64)):
        if off < len(b):
            print("   %-22s %-16s 0x%07x: %s" % (f[:16], lbl, off,
                  re.sub(rb"[^\x20-\x7e]", b".", b[off:off + 0x40]).decode()))
    for nm in ("CSEQEV", "CSEQAL", "CSEQMX", "CSEQFL", "CAPSVC"):
        p = nm.encode("utf-16-le")
        hits = [m.start() for m in re.finditer(re.escape(p), b)]
        a = [m.start() for m in re.finditer(re.escape(nm.encode()), b)]
        if hits or a:
            print("      %-8s utf16=%d %s  ascii=%d %s" %
                  (nm, len(hits), ["0x%x" % h for h in hits[:4]],
                   len(a), ["0x%x" % h for h in a[:4]]))
