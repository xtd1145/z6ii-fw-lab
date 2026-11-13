"""Independent re-verification of the mode/readout table the preshoot agent cited.

vma 0x10985bdc -> file offset 0x985bdc in extracted/eg1850_mas_01700 (base 0x10000000).
"""
import numpy as np

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
raw = open(MAS, "rb").read()
A = 0x985BDC

# geometry constants we care about
GEOS = {6048: "FXw", 4024: "FXh", 4016: "1:1w", 2680: "1:1ish", 3936: "DXw",
        2624: "DXh", 2944: "Mw", 1968: "Mh", 1920: "FHDw", 1080: "FHDh",
        3840: "UHDw", 2160: "UHDh", 4032: "FXh?", 5104: "5104", 8064: "8064",
        2048: "2048", 1280: "1280", 720: "720", 480: "480", 576: "576"}


def fields(off, stride, n, label):
    print("\n== %s  off=0x%08x stride=%d n=%d" % (label, off, stride, n))
    for i in range(n):
        o = off + i * stride
        u16 = np.frombuffer(raw[o:o + stride], dtype="<u2")
        u32 = np.frombuffer(raw[o:o + stride // 2 * 2], dtype="<u4") if False else \
            np.frombuffer(raw[o:o + min(stride, 40)], dtype="<u4")
        geo = [GEOS[int(x)] for x in u16 if int(x) in GEOS]
        fps = [int(x) for x in u16 if int(x) in (24, 25, 30, 45, 50, 60, 100, 120)]
        print("   rec%2d off=%08x  u16=%s" % (i, o, " ".join("%5d" % x for x in u16)))
        print("          u32=%s  geo=%s fps=%s"
              % (" ".join("%10d" % x for x in u32), ",".join(geo) or "-", fps or "-"))


# record stride unknown: try 0x1C and 0x30, the two spacings the agent's offsets imply
fields(A, 0x1c, 12, "stride 0x1c (agent: 0x10985bdc..0x10985d10)")
fields(0x985e58, 0x1c, 4, "agent's 3 records at 0x10985e58/e74/e90, stride 0x1c")
