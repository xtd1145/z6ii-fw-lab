"""Hunt the still-frame-rate cap table near the mode-table neighbourhood."""
import numpy as np, re, collections

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
raw = open(MAS, "rb").read()
Z8 = r"D:\z6ii_fw_lab\all_modules\Z_8_0311\eg2070_0311b0.bi"
z8 = open(Z8, "rb").read()

FPS = [1, 2, 3, 5, 6, 8, 10, 12, 14, 15, 16, 18, 20, 24, 25, 30, 40, 45, 50, 60, 100, 120]


def windows(blob, lo, hi, name):
    print("\n== %s  [%08x-%08x]" % (name, lo, hi))
    seg = blob[lo:hi]
    u8 = np.frombuffer(seg, dtype="u1").astype(np.int32)
    hit = np.isin(u8, FPS)
    # score each 16-byte aligned window by how many distinct ladder values it holds
    W = 16
    n = len(u8) // W
    sc = hit[:n * W].reshape(n, W)
    rows = []
    for i in range(n):
        vals = sc[i]
        big = int(((vals == 30) | (vals == 45) | (vals == 60) | (vals == 120)).sum())
        if big and vals.sum() >= 300:
            rows.append((lo + i * W, [int(x) for x in vals]))
    print("   candidate windows:", len(rows))
    for off, vals in rows[:40]:
        print("   %08x  %s" % (off, " ".join("%3d" % v for v in vals)))


def u16_ladder(blob, lo, hi, name):
    print("\n== %s u16 ladder pass [%08x-%08x]" % (name, lo, hi))
    n = (hi - lo) // 2
    u = np.frombuffer(blob[lo:lo + n * 2], dtype="<u2").astype(np.int64)
    ok = np.isin(u, FPS)
    runs, i = [], 0
    while i < n:
        if ok[i]:
            j = i
            while j < n and ok[j]:
                j += 1
            if j - i >= 4:
                runs.append((lo + i * 2, [int(x) for x in u[i:j]]))
            i = j
        else:
            i += 1
    for off, vals in runs[:30]:
        has45 = 45 in vals
        print("   %s%08x  %s" % (">" if has45 else " ", off,
                                 " ".join(str(v) for v in vals[:24])))
    print("   runs:", len(runs), " with-45:", sum(1 for _, v in runs if 45 in v))


print("count of literal byte 45 in mas:", raw.count(b"\x2d"))
u16_45 = [m.start() for m in re.finditer(re.escape(b"\x2d\x00"), raw)]
print("u16 LE value 45 aligned hits:", len([o for o in u16_45 if not o & 1]))

windows(raw, 0x92C000, 0x946000, "mas mode neighbourhood (u8)")
u16_ladder(raw, 0x92C000, 0x946000, "mas")
u16_ladder(z8, 0, min(len(z8), 0x400000), "Z8 eg2070 head")
