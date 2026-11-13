"""Read-only firmware helpers for the DX45 JPEG line.

mas: file_off + 0x10000000 = VMA
sla: file_off + 0x10000000 = VMA  (VERIFY below, see verify_sla_base)
"""
import struct, os

ROOT = r"D:\z6ii_fw_lab"
MOD = {
    "mas": os.path.join(ROOT, "all_modules", "Z6_2_0170", "eg1850_mas_01700"),
    "sla": os.path.join(ROOT, "all_modules", "Z6_2_0170", "eg1850_sla_01700"),
}
_cache = {}


def load(name):
    if name not in _cache:
        with open(MOD[name], "rb") as f:
            _cache[name] = f.read()
    return _cache[name]


def size(name):
    return len(load(name))


def v2f(vma):
    return vma - 0x10000000


def f2v(off):
    return off + 0x10000000


def u8(name, off):
    return load(name)[off]


def u16(name, off):
    return struct.unpack_from("<H", load(name), off)[0]


def u32(name, off):
    return struct.unpack_from("<I", load(name), off)[0]


def s32(name, off):
    return struct.unpack_from("<i", load(name), off)[0]


def hexdump(name, off, n, base_note=""):
    b = load(name)
    lines = []
    for i in range(0, n, 16):
        chunk = b[off + i: off + i + 16]
        hx = " ".join("%02x" % c for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        lines.append("%08x (+%06x)  %-47s  %s" % (f2v(off + i), i, hx, asc))
    return "\n".join(lines)


def find_all(name, pat, align=1):
    b = load(name)
    out = []
    i = b.find(pat)
    while i >= 0:
        if align <= 1 or i % align == 0:
            out.append(i)
        i = b.find(pat, i + 1)
    return out


def blx_targets(name, start, end):
    """Scan Thumb BL/BLX (F000 F800 pairs) in [start,end) and yield (addr, target)."""
    b = load(name)
    out = []
    a = start
    while a + 4 <= end:
        hw1 = struct.unpack_from("<H", b, a)[0]
        hw2 = struct.unpack_from("<H", b, a + 2)[0]
        if (hw1 & 0xF800) == 0xF000 and (hw2 & 0xD000) in (0xD000, 0xC000, 0xE000):
            s = (hw1 >> 10) & 1
            j1 = (hw2 >> 13) & 1
            j2 = (hw2 >> 11) & 1
            i1 = 1 - (j1 ^ s)
            i2 = 1 - (j2 ^ s)
            imm10 = hw1 & 0x3FF
            imm11 = hw2 & 0x7FF
            off = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
            if off & 0x1000000:
                off -= 0x2000000
            tgt = (a + 4 + off) & 0xFFFFFFFF
            out.append((a, tgt))
            a += 4
        else:
            a += 2
    return out


if __name__ == "__main__":
    for k in MOD:
        print(k, size(k), hex(size(k)))
