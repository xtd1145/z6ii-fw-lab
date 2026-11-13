"""Adversarial verification of the agent's mode-table claims.

Claim 1: vma 0x10985bdc (file 0x985bdc) is a stride-0x1c readout-mode table whose
         id 9 = 60fps 3936x2624 and id 10 = 60fps 4016x2680, ADC 787.5 MHz, lane 6->8.
Claim 2: vma 0x1092d1c8 is a stride-8 mode id 1..12 table with fps at +8.
"""
import math, collections, re
import numpy as np

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
raw = open(MAS, "rb").read()


def ent(b):
    if not b:
        return 0.0
    c = collections.Counter(b)
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in c.values())


print("[A] is 0x985bdc structured at all?")
for span in (0x100, 0x400, 0x1000):
    seg = raw[0x985bdc:0x985bdc + span]
    print("    off 0x985bdc +0x%04x  entropy %.3f  printable %.3f"
          % (span, ent(seg), sum(32 <= x < 127 for x in seg) / len(seg)))
print("    reference: whole-image entropy %.3f, a real table region %.3f"
      % (ent(raw), ent(raw[0x941400:0x941400 + 0x400])))

print("\n[B] claim 2: 0x92d1c8 stride 8, mode id 1..12, fps at +8")
u = np.frombuffer(raw[0x92d1c8:0x92d1c8 + 12 * 8], dtype="<u2")
print("    u16:", " ".join("%5d" % x for x in u))
u32 = np.frombuffer(raw[0x92d1c8:0x92d1c8 + 12 * 8], dtype="<u4")
print("    u32:", " ".join("%10d" % x for x in u32))

print("\n[C] global search: any (w,h,fps) triple with a DX/FX geometry and fps>30")
GEOS = [(6048, 4024), (6048, 4032), (3936, 2624), (4016, 2680), (3936, 2216),
        (2944, 1968), (1968, 1312), (3840, 2160), (1920, 1080), (6048, 3400)]
FPS = (40, 45, 50, 60, 90, 100, 120, 150, 240)
found = 0
for w, h in GEOS:
    bw, bh = np.frombuffer(raw, dtype="<u2", count=len(raw) // 2), None
    arr = np.frombuffer(raw, dtype="<u2", count=len(raw) // 2)
    idx = np.where(arr == w)[0]
    idx = idx[~(idx & 1)]
    for i in idx:
        o = int(i) * 2
        window = arr[o // 2:o // 2 + 12]
        if not (window == h).any():
            continue
        hits = [int(x) for x in window if int(x) in FPS]
        if hits:
            print("    off=0x%08x %dx%d fps-candidates=%s" % (o, w, h, hits))
            found += 1
            if found > 40:
                break
    if found > 40:
        break
print("   total geometry+high-fps co-occurrences:", found)

print("\n[D] what the honest readout ceiling evidence is (video table 0x92cf50)")
for i in range(18):
    o = 0x92cf50 + i * 12
    a, b, c, d = np.frombuffer(raw[o:o + 8], dtype="<u2")
    print("    off=0x%08x  %5d %5d %5d %5d" % (o, a, b, c, d))
