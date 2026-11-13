"""Dump the Z6 II mode/geometry table region as structured columns."""
import numpy as np

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
raw = open(MAS, "rb").read()


def grid(a, b, stride=16, label=""):
    print("\n== %s  [0x%08x .. 0x%08x]  stride %d u16" % (label, a, b, stride))
    n = (b - a) // 2
    u = np.frombuffer(raw[a:a + n * 2], dtype="<u2")
    for i in range(0, n, stride):
        row = u[i:i + stride]
        print("  %08x  " % (a + i * 2) + " ".join("%5d" % x for x in row))


def bagrid(a, b, stride=8, label=""):
    print("\n== %s  [0x%08x]  as BE16 stride %d" % (label, a, stride))
    n = (b - a) // 2
    u = np.frombuffer(raw[a:a + n * 2], dtype=">u2")
    for i in range(0, n, stride):
        print("  %08x  " % (a + i * 2) + " ".join("%5d" % x for x in u[i:i + stride]))


grid(0x9414C0, 0x941900, 16, "mode/geometry ladder (LE)")
bagrid(0x9414C0, 0x941900, 16, "same region, big-endian view")
