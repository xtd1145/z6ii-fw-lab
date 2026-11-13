#!/usr/bin/env python3
"""Core load accounting, clean pass (read-only).

(a) real VMA base + peripheral ownership, filler-pattern filtered
(b) the linked-in source-file inventory per core -> the actual work split
"""
import re, sys
from collections import Counter, defaultdict

M, S = "extracted/eg1850_mas_01700", "extracted/eg1850_sla_01700"
FILL = {b * 0x01010101 for b in range(256)}


def words(b):
    return memoryview(b).cast("I")


def code_mask(img, per=1 << 16, thr=40):
    pro = (b"\x2d\xe9", b"\xb5\xb0", b"\xb4\xb0", b"\x2d\xe8")
    out = []
    for off in range(0, len(img), per):
        ch = img[off:off + per]
        out.append(off if sum(ch.count(p) for p in pro) > thr else -1)
    return out


def base_of(img, cmask):
    c = Counter()
    for off in cmask:
        if off < 0:
            continue
        for x in words(img[off:off + (1 << 16)]):
            if x in FILL or x == 0:
                continue
            if 0x1000_0000 <= x < 0x2000_0000:
                c[x >> 20] += 1
    if not c:
        return None, c
    return min(t << 20 for t, n in c.items() if n >= max(c.values()) // 4), c


REGIONS = [("MMIO", 0x40000000, 0x50000000), ("DRAM", 0xC0000000, 0xD0000000),
           ("SRAM", 0x20000000, 0x30000000), ("XMAP", 0x80000000, 0xA0000000),
           ("HIGH", 0x10000000, 0x20000000)]


def ownership(img, lo, hi, cmask):
    blk = Counter()
    for off in cmask:
        if off < 0:
            continue
        for x in words(img[off:off + (1 << 16)]):
            if x in FILL:
                continue
            if lo <= x < hi:
                continue
            for nm, a, b in REGIONS:
                if a <= x < b:
                    blk[(nm, x >> 16)] += 1
                    break
    return blk


SRC = re.compile(rb"[A-Za-z][A-Za-z0-9_+.\-]{3,60}\.(?:c|cpp|h)\x00")


def srcs(img):
    return {m.group(0)[:-1].decode() for m in SRC.finditer(img)}


def main():
    mas, sla = open(M, "rb").read(), open(S, "rb").read()
    cm, cs = code_mask(mas), code_mask(sla)
    mb, mc = base_of(mas, cm)
    sb, sc = base_of(sla, cs)
    print(f"mas code blocks={sum(1 for x in cm if x>=0)}  VMA~0x{mb:08X}  end=0x{0x10000000+len(mas):08X}")
    print(f"sla code blocks={sum(1 for x in cs if x>=0)}  VMA~0x{sb:08X}  end=0x{sb+len(sla):08X}")
    gap = sb - (0x10000000 + len(mas))
    print(f"  gap mas_end -> sla_base = {gap:+,}  ({'contiguous' if abs(gap) < (1<<20) else 'separate'})")

    mo = ownership(mas, mb, mb + len(mas), cm)
    so = ownership(sla, sb, sb + len(sla), cs)
    print("\n=== peripheral blocks touched from CODE (filler-filtered, 64KB granularity) ===")
    for nm, d in (("mas", mo), ("sla", so)):
        tot = defaultdict(int)
        for (r, _), n in d.items():
            tot[r] += n
        print(f"  {nm}: distinct blocks={len(d)}  " + "  ".join(f"{k}={v}" for k, v in sorted(tot.items())))
    inter = set(mo) & set(so)
    mas_only = set(mo) - set(so)
    sla_only = set(so) - set(mo)
    print(f"  shared blocks={len(inter)}  mas-only={len(mas_only)}  sla-only={len(sla_only)}")
    print("  top sla-dominant blocks (sla count > mas count):")
    dom = [(k, so[k], mo.get(k, 0)) for k in set(so) if so[k] > mo.get(k, 0)]
    for k, a, b in sorted(dom, key=lambda t: -(t[1] - t[2]))[:10]:
        print(f"     {k[0]} 0x{k[1]:08X}0000  sla={a} mas={b}")

    A, B = srcs(mas), srcs(sla)
    print(f"\n=== linked-in source files: mas={len(A)}  sla={len(B)}  shared={len(A&B)} ===")
    KEY = ["JPEG", "Jpeg", "H264", "HEVC", "Mpeg", "Movie", "Avi", "Isp", "Raw", "NEF", "Sensor",
           "Af", "AF", "Lens", "Card", "Sd", "Mmc", "Usb", "Lv", "Live", "View", "Lcd", "Oled",
           "Key", "Btn", "Power", "Bat", "Nvm", "Flash", "I2c", "Spi", "Uart", "Dma", "Timer",
           "Shutter", "Wb", "Denoise", "Jpeg", "Encode", "Decode", "Resize", "Rotate", "Gimbal",
           "Vr", "Ibis", "Audio", "Mic", "Hdmi", "Wifi", "Bt", "Gps", "Task", "Os", "Sched",
           "Debug", "Dbg", "Mem", "Cache", "Img", "Proc", "Pipe", "Queue", "Buf", "Fifo"]
    def cls(f):
        s = f.split("/")[-1]
        for k in KEY:
            if k.lower() in s.lower():
                return k
        return "other"
    for label, s_ in (("mas-only", A - B), ("sla-only", B - A)):
        g = defaultdict(list)
        for f in s_:
            g[cls(f)].append(f)
        print(f"\n-- {label} ({len(s_)} files) --")
        for k, v in sorted(g.items(), key=lambda t: -len(t[1]))[:22]:
            ex = ", ".join(sorted(x.split('/')[-1] for x in v)[:4])
            print(f"   [{k}] x{len(v)}  {ex}")
    print(f"\n-- shared library files ({len(A&B)}), sample --")
    print("   " + ", ".join(sorted(x.split('/')[-1] for x in (A & B))[:25]))


if __name__ == "__main__":
    sys.exit(main())
