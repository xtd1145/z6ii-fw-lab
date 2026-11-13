"""Find every A32 site that reaches a DDR control-block word through a PC pool.

The boot path aborts because cells in the 0xc18ad8xx block do not hold what the
firmware expects (e.g. 0xc18ad858 is read as a table pointer but holds 0x1000).
Rather than hunt for the writer in a 38 MB image by hand, test every aligned
word in a code range against the pc-relative `ldr` encoding and resolve the
pool slot it reads:

    ldr Rt,[pc,#imm]  ->  cond 0 1 L 1 P 1 0 0 1 1111 Rt imm12

Words are tested one at a time instead of being streamed through capstone,
because a linear sweep resynchronises badly across the tables and zero fill
that a firmware image is full of: capstone happily decodes 0x00000000 as
`andeq r0,r0,r0` and stays "valid" while drifting out of phase.

  python find_refs.py --lo 0x10100000 --hi 0x10180000 \
      --targets 0xc18ad840..0xc18ad8e0
"""
import argparse

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
MAS_VA = 0x10020000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=lambda x: int(x, 16), required=True)
    ap.add_argument("--hi", type=lambda x: int(x, 16), required=True)
    ap.add_argument("--targets", required=True,
                    help="comma separated addr or lo..hi windows")
    ap.add_argument("--writers", action="store_true",
                    help="also report the first store through the loaded "
                    "register after each pool load")
    args = ap.parse_args()

    wanted = []
    for spec in args.targets.split(","):
        if ".." in spec:
            lo, hi = spec.split("..")
            wanted.append((int(lo, 16), int(hi, 16)))
        else:
            v = int(spec, 16)
            wanted.append((v, v + 4))

    blob = open(MAS, "rb").read()

    def word(va):
        return int.from_bytes(blob[va - MAS_VA: va - MAS_VA + 4], "little")

    def hit(v):
        return any(lo <= v < hi for lo, hi in wanted)

    pools = {}
    for off in range(0, len(blob) - 4, 4):
        va = off + MAS_VA
        if hit(word(va)):
            pools[va] = word(va)
    print("pool slots holding a target value: %d" % len(pools))

    refs = []
    for addr in range(args.lo, args.hi, 4):
        w = word(addr)
        if (w & 0x051F0000) != 0x051F0000:
            continue
        imm = w & 0xfff
        # U (bit 23) selects the sign; ARM's pc is the instruction address + 8.
        pool = addr + 8 + (imm if (w >> 23) & 1 else -imm)
        if pool in pools:
            refs.append((addr, w, pool))

    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    for addr, w, pool in refs:
        text = "?"
        for insn in md.disasm(blob[addr - MAS_VA: addr - MAS_VA + 4], addr):
            text = insn.mnemonic + " " + insn.op_str
        cond = w >> 28
        print("0x%08x  %-26s pool 0x%08x -> 0x%08x%s" % (
            addr, text, pool, pools[pool], "" if cond == 0xE else
            "  (cond 0x%x)" % cond))
    print("reaching instructions: %d" % len(refs))

    if not args.writers:
        return

    # A cell that holds a pointer is seeded by a store through the very register
    # the pool load above filled, so follow each load for a few words and report
    # the first plain store whose base is that register:
    #   STR imm: cond 0101 1000 Rn Rt imm12   (P=1,U=1,W=0,B=0,L=0)
    print("--- stores through the loaded register ---")
    for addr, w, pool in refs:
        rt = (w >> 12) & 0xf
        for off in range(addr + 4, min(addr + 4 * 24, args.hi), 4):
            s = word(off)
            if (s & 0x0FF0F000) == 0x05800000 and ((s >> 16) & 0xf) == rt:
                text = "?"
                for insn in md.disasm(blob[off - MAS_VA: off - MAS_VA + 4], off):
                    text = insn.mnemonic + " " + insn.op_str
                print("0x%08x pool -> 0x%08x ; 0x%08x  %-22s disp 0x%x" % (
                    addr, pools[pool], off, text, s & 0xfff))
            elif (s & 0x0FF0F000) == 0x05900000 and ((s >> 16) & 0xf) == rt:
                text = "?"
                for insn in md.disasm(blob[off - MAS_VA: off - MAS_VA + 4], off):
                    text = insn.mnemonic + " " + insn.op_str
                print("0x%08x pool -> 0x%08x ; 0x%08x  %-22s disp 0x%x" % (
                    addr, pools[pool], off, text, s & 0xfff))
            if (s & 0x0FFFFFFF) == 0x012FFF1E:  # bx lr ends the accessor
                break


if __name__ == "__main__":
    main()
