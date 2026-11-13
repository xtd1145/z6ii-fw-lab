#!/usr/bin/env python3
"""Derive the nikon-z6ii machine memory map from the mas translation table.

Ground truth used
  TTBR0 = 0x10030000 (mcr p15,0,r0,c2,c0,0 at mas+0xbcf4) and the table sits at
  file offset 0x10000, so entry i covers VA i*1MiB and base = 0x10020000.

Bit layout
  Only word[31:20] (PA) and the descriptor type in word[1:0] are layout stable,
  so classification uses those plus the XN bit exactly where QEMU reads it
  (target/arm/ptw.c get_phys_addr_v6: xn = word[4], type = word[1:0]).

Rule
  A PA is RAM-backed when some VA maps it from a section QEMU treats as
  executable (XN clear). Everything the firmware marks XN is an MMIO window and
  becomes an unimplemented-device so that `-d unimp` records what it pokes.
"""
import json
import struct
import sys

ROOT = r"D:\z6ii_fw_lab"
MAS = ROOT + r"\extracted\eg1850_mas_01700"
TBL_OFF = 0x10000
TBL_VA = 0x10030000
BASE = TBL_VA - TBL_OFF
MB = 1 << 20

blob = open(MAS, "rb").read()
words = struct.unpack_from("<4096I", blob, TBL_OFF)

kinds = {}
sections = []
for i, w in enumerate(words):
    kind = (w & 3, bool(w & (1 << 18)))
    kinds[kind] = kinds.get(kind, 0) + 1
    if w == 0:
        continue
    if w & 3 != 2:
        continue
    sections.append((i, w, w & 0xFFF00000, (w >> 4) & 1))

print("descriptor types (type,bit18) -> count")
for k in sorted(kinds):
    print("  %s : %d" % (k, kinds[k]))
print("sections: %d  used entries: %d / 4096" % (len(sections),
                                                 sum(1 for w in words if w)))

# PA -> set of (va, xn) mappings, merged into runs per VA.
ram_spans = []
dev_spans = []
va_runs = []
i = 0
while i < len(sections):
    idx, w, pa, xn = sections[i]
    n = 1
    while i + n < len(sections):
        idx2, w2, pa2, xn2 = sections[i + n]
        if idx2 != idx + n or pa2 != pa + n * MB or w2 != w:
            break
        n += 1
    va_runs.append(dict(va=idx * MB, pa=pa, size=n * MB, xn=xn, word=w))
    span = (pa, pa + n * MB)
    (dev_spans if xn else ram_spans).append(span)
    i += n


def merge(spans):
    out = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


merged_ram = merge(ram_spans)
ram_pages = set()
for lo, hi in merged_ram:
    ram_pages.update(range(lo, hi, MB))

# A VA run is only an MMIO window if none of its PA is RAM-backed elsewhere.
merged_dev = merge([s for s in dev_spans if s[0] not in ram_pages])

print("\nRAM (some VA maps it non-XN):")
tot = 0
for lo, hi in merged_ram:
    print("  %08x..%08x  %7.1f MiB" % (lo, hi, (hi - lo) / 1048576))
    tot += hi - lo
print("  total %.1f MiB" % (tot / 1048576))

print("\nXN-only PA windows (candidate MMIO):")
for lo, hi in merged_dev:
    print("  %08x..%08x  %7.1f MiB" % (lo, hi, (hi - lo) / 1048576))

print("\nmodule coverage (VA 0x%08x..0x%08x):" % (BASE, BASE + len(blob)))
for r in va_runs:
    if r["va"] + r["size"] <= BASE or r["va"] >= BASE + len(blob):
        continue
    print("  VA %08x..%08x -> PA %08x..%08x %6.1f MiB xn=%d word=%08x"
          % (r["va"], r["va"] + r["size"] - 1, r["pa"], r["pa"] + r["size"] - 1,
             r["size"] / 1048576, r["xn"], r["word"]))

alias = [r for r in va_runs if r["va"] != r["pa"]]
print("\nnon-identity runs: %d" % len(alias))
for r in alias:
    print("  VA %08x..%08x -> PA %08x..%08x %6.1f MiB xn=%d"
          % (r["va"], r["va"] + r["size"] - 1, r["pa"], r["pa"] + r["size"] - 1,
             r["size"] / 1048576, r["xn"]))

out = dict(source_module="eg1850_mas_01700", blob_base_va=BASE,
           ttbr=TBL_VA, dacr=0x55555555, table_file_offset=TBL_OFF,
           ram_regions=[dict(base=lo, size=hi - lo) for lo, hi in merged_ram],
           device_regions=[dict(base=lo, size=hi - lo) for lo, hi in merged_dev],
           va_runs=[dict(va=r["va"], pa=r["pa"], size=r["size"], xn=r["xn"])
                    for r in va_runs])
path = ROOT + r"\work\qemu_build\z6ii_memmap.json"
json.dump(out, open(path, "w"), indent=1)
print("\nwrote", path)
sys.exit(0)
