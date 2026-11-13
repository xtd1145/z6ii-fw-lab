"""DX 45fps / 30fps ladder hunt -- read-only scan of the decrypted Z6 II image."""
import re
import numpy as np, struct, sys, collections

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
SLA = r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700"
raw = open(MAS, "rb").read()
print("mas %d bytes" % len(raw))

# ---- 1. real Z6 II geometry constants (u16 LE) -------------------------------
GEO = {"FX L": (6048, 4024), "DX L": (3936, 2624), "DX 16:9": (3936, 2210),
       "FX 16:9": (6048, 3402), "UHD": (3840, 2160), "FHD": (1920, 1080),
       "DX S": (1024, 680)}
for name, (w, h) in GEO.items():
    for tag, val in (("w", w), ("h", h)):
        b = struct.pack("<H", val)
        offs = [m.start() for m in re.finditer(
            re.escape(b), raw)]
        # only even-aligned hits are plausible table entries
        al = [o for o in offs if not o & 1]
        print("  %-8s %s=%-5d %10d hits (%d aligned)" % (name, tag, val, len(offs), len(al)))

# ---- 2. paired (w,h) tables: DX 3936 followed within +-16 B by 2624/2210 ----
print("\n== paired geometry tables (3936 near 2624/2210/680) ==")
W = struct.pack("<H", 3936)
cands = []
for m in re.finditer(re.escape(W), raw):
    o = m.start()
    if o & 1:
        continue
    for hv in (2624, 2210, 680, 176, 480):
        hb = struct.pack("<H", hv)
        j = raw.find(hb, max(0, o - 16), o + 16)
        if j >= 0 and not j & 1:
            cands.append((o, hv))
            break
clusters, prev = [], None
for o, hv in sorted(set(cands)):
    if prev is None or o - prev > 0x60:
        clusters.append([])
    clusters[-1].append((o, hv))
    prev = o
print("   %d candidate hits in %d clusters" % (len(cands), len(clusters)))
for cl in clusters[:14]:
    a = cl[0][0] & ~0xF
    seg = raw[a:a + 0x60]
    u = np.frombuffer(seg[:len(seg) // 2 * 2], dtype="<u2")
    print("   --- off 0x%08x (vma 0x%08x)" % (a, 0x10000000 + a))
    print("      ", " ".join("%d" % x for x in u[:44]))
