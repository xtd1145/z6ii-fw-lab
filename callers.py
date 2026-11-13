import sys, struct, numpy as np

BASE = 0x10000000


def bl_targets(data, mode="thumb"):
    if mode == "thumb":
        D = np.frombuffer(data, dtype=np.uint16)
        D = D[: len(D) - 1]
        a = D[:-1].astype(np.int64)
        b = D[1:].astype(np.int64)
        p = np.arange(len(a))
        m = ((a & 0xF800) == 0xF000) & ((b & 0xD000) == 0xD000)
        S = (a >> 10) & 1
        imm10 = a & 0x3FF
        J1 = (b >> 13) & 1
        J2 = (b >> 11) & 1
        imm11 = b & 0x7FF
        I1 = 1 - (J1 ^ S)
        I2 = 1 - (J2 ^ S)
        imm = (S << 23) | (I1 << 22) | (I2 << 21) | (imm10 << 11) | imm11
        imm = np.where(S == 1, imm - (1 << 24), imm)
        tgt = (BASE + p * 2 + 4) + imm * 2
        return tgt[m], (BASE + p * 2)[m]
    w = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.uint32)
    p = np.arange(len(w), dtype=np.int64)
    m = (w & 0xFE000000) == 0xFA000000
    off = (w & 0x00FFFFFF).astype(np.int64)
    off = np.where(off & 0x800000, off - 0x1000000, off) << 2
    return (BASE + p * 4 + 8 + off)[m], (BASE + p * 4)[m]


if __name__ == "__main__":
    path = sys.argv[1]
    want = int(sys.argv[2], 16)
    mode = sys.argv[3] if len(sys.argv) > 3 else "thumb"
    data = open(path, "rb").read()
    tg, frm = bl_targets(data, mode)
    idx = np.where(tg == want)[0]
    print(f"callers of {want:#x} ({mode}): {len(idx)}")
    for i in idx:
        print(f"  from {int(frm[i]):#010x}  (file off {int(frm[i]) - BASE:#x})")
