"""Static inventory of the device addresses mas can reach.

The -d unimp trace only shows what the boot path touched before it stopped. This
walks the image's pc-relative literal loads -- `ldr Rt,[pc,#imm]`, the only way
A32 code materialises a 32-bit address it does not build with movw/movt -- and
histograms the words they fetch that fall inside a window the firmware's own
page table marks XN. Counting every aligned word instead is useless: the image
holds embedded blobs whose bytes look like addresses by the million.

  python mmio_inventory.py --min 2
"""
import argparse
import struct
from collections import Counter

MAS = r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
MAS_VA = 0x10020000

# Windows the decoded page table marks XN, plus the module load region itself.
WINDOWS = [
    ("peri0", 0x04000000, 0x04010000),
    ("peri1", 0x04010000, 0x04400000),
    ("peri2", 0x20000000, 0x30000000),
    ("peri3", 0x80000000, 0x80100000),
    ("high", 0xa0000000, 0x100000000),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=3, help="min references")
    ap.add_argument("--gran", type=int, default=0x1000,
                    help="bucket size for the printed addresses")
    args = ap.parse_args()

    blob = open(MAS, "rb").read()
    words = struct.unpack_from("<%dI" % (len(blob) // 4), blob)
    code_lo, code_hi = MAS_VA, MAS_VA + len(blob)

    per_window = Counter()
    per_addr = Counter()
    sites = {}
    # ldr Rt,[pc,#imm]: bits 27,25,24,23,22,21,20 set as template 0x051f0000
    # with Rn (19:16) = 1111 and L=1 (bit 20).
    for i, w in enumerate(words):
        if (w & 0x051F0010) != 0x051F0000:
            continue
        addr = MAS_VA + i * 4
        imm = w & 0xfff
        pool = addr + 8 + (imm if (w >> 23) & 1 else -imm)
        if not (code_lo <= pool < code_hi):
            continue
        v = words[(pool - MAS_VA) // 4]
        for name, lo, hi in WINDOWS:
            if lo <= v < hi:
                per_window[name] += 1
                bucket = (v // args.gran) * args.gran
                per_addr[bucket] += 1
                sites.setdefault(bucket, addr)
                break

    print("references into the XN windows:")
    for name, n in per_window.most_common():
        print("  %-6s %7d" % (name, n))

    print("\naddress buckets (granularity 0x%x), top 60:" % args.gran)
    for addr, n in per_addr.most_common(60):
        if n >= args.min:
            print("  0x%08x  %6d   first ldr at 0x%08x" % (addr, n, sites[addr]))


if __name__ == "__main__":
    main()
