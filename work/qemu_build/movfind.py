"""Find movw/movt pairs that materialise a DDR control-block address.

find_refs.py only follows `ldr Rt,[pc,#imm]` pools. A firmware that seeds a
global by hand writes it as

    movw rX, #:lower16:0xc18ad858
    movt rX, #:upper16:0xc18ad858
    str  rY, [rX]

so test every aligned word against the two encodings and report the stores that
ride on the register afterwards:

    MOVW: cond 0000 0100 0 hw=0 i=1 imm4 Rd imm8     -> 0x004?0000 mask 0x0FFF0000
    MOVT: cond 0000 1100 0 hw=0 i=1 imm4 Rd imm8     -> 0x00C?0000

  python movfind.py --lo 0x10020000 --hi 0x10200000 --want 0xc18ad800..0xc18ad900
"""
import argparse

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs

IMAGES = {
    "mas": (r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700", 0x10020000),
    "sla": (r"D:\z6ii_fw_lab\extracted\eg1850_sla_01700", 0x10020000),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="mas", choices=sorted(IMAGES))
    ap.add_argument("--lo", type=lambda x: int(x, 16), required=True)
    ap.add_argument("--hi", type=lambda x: int(x, 16), required=True)
    ap.add_argument("--want", required=True, help="addr or lo..hi window")
    args = ap.parse_args()

    path, base = IMAGES[args.image]
    blob = open(path, "rb").read()

    def word(va):
        return int.from_bytes(blob[va - base: va - base + 4], "little")

    if ".." in args.want:
        lo, hi = (int(x, 16) for x in args.want.split(".."))
    else:
        lo = int(args.want, 16)
        hi = lo + 4

    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)

    def text(addr):
        for insn in md.disasm(blob[addr - base: addr - base + 4], addr):
            return "%s %s" % (insn.mnemonic, insn.op_str)
        return "?"

    n = 0
    for addr in range(args.lo, args.hi, 4):
        w = word(addr)
        if (w & 0x0FFF0000) not in (0x00400000, 0x00C00000):
            continue
        rd = (w >> 12) & 0xf
        imm = ((w & 0xf0000) >> 4) | (w & 0xff)
        if (w & 0x00C00000) == 0x00C00000:      # movt: patch the top half
            continue
        nxt = word(addr + 4)
        if (nxt & 0x0FF0FFF0) != (0x00C00000 | (rd << 12)):
            continue
        if (nxt & 0xf) != ((nxt >> 16) & 0xf):
            continue
        imm |= ((nxt >> 16) & 0xf) << 16
        if not lo <= imm < hi:
            continue
        n += 1
        print("0x%08x  %s ; %s   -> 0x%08x" % (addr, text(addr),
                                               text(addr + 4), imm))
        for off in range(addr + 8, addr + 8 * 4, 4):
            s = word(off)
            # STR/LDR immediate, P=1 U=1 W=0, base == rd
            if (s & 0x0FF0F000) in (0x05800000, 0x05900000) and \
               ((s >> 16) & 0xf) == rd:
                print("        0x%08x  %s   disp 0x%x" % (
                    off, text(off), s & 0xfff))
    print("movw/movt pairs in the window: %d" % n)


if __name__ == "__main__":
    main()
