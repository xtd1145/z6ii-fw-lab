"""Thumb-2 far-branch encoder, validated against the cracked Z6 firmware's own hooks.

Why validate this way: the WL patch gives us real (site, target) -> 4-byte pairs produced
by a working flashable patch. If our encoder reproduces those bytes bit-for-bit, the
Z6 II hook generator built on it is correct by construction.

Usage:  python work/preshoot_build/thumb_enc.py            # run validation
        python -c "from work.preshoot_build.thumb_enc import b24; ..."
"""
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STOCK_Z6 = ROOT / 'all_modules/Z_6_0380/eg1620_038000.bi'
PATCH_Z6 = ROOT / 'work/wlz6/EG162080_en.BIN'
BASE = 0x10020000                     # proven in work/wlmech/MECHANISM.md §0

# site_file_off -> target_file_off, from work/wlmech/regions.tsv class H/D hooks
HOOK_VECTORS = [
    (0x04B1AC, 0x00E57C, 4),
    (0x0525B0, 0x00DF58, 4),
    (0x1C5BA8, 0x00E4C8, 4),
    (0x21AD20, 0x00E608, 4),
    (0x247936, 0x00DFA0, 4),
    (0x3DF942, 0x00E590, 4),
    (0x3E5304, 0x00E5A4, 4),
    (0x4AC0EE, 0x21AD20, 4),
    (0x0F20F0, 0x00DF80, 4),
]
# bl forms (link, so the second halfword differs)
BL_VECTORS = [
    (0x0F20E2, 0x00D884, 4),
    (0x46A150, 0x00E5B8, 4),
]


def _hw(v):
    return struct.pack('<H', v)


def b24(site_vma, target_vma, link=False):
    """Encode Thumb-2 B.W / BL.W. Both addresses are VMAs (target must be even-aligned
    relative to its own 2-byte boundary; the +1 Thumb bit is implicit)."""
    imm = target_vma - (site_vma + 4)
    if not -(1 << 24) <= imm < (1 << 24):
        raise ValueError(f'b.w displacement out of range: {imm:#x}')
    s = 1 if imm < 0 else 0
    v = imm & ((1 << 25) - 1)
    i10 = (v >> 12) & 0x3FF
    i11 = (v >> 1) & 0x7FF
    i23 = (v >> 23) & 1
    i22 = (v >> 22) & 1
    j1 = 1 ^ (i23 ^ s)
    j2 = 1 ^ (i22 ^ s)
    hw1 = 0xF000 | (s << 10) | i10
    hw2 = 0x8000 | (j1 << 13) | (1 << 12) | (j2 << 11) | i11
    if link:
        hw2 |= 1 << 14                                  # BL: '11' prefix
    return _hw(hw1) + _hw(hw2)


def validate(verbose=True):
    patch = PATCH_Z6.read_bytes()
    stock = STOCK_Z6.read_bytes()
    bad = []
    for site, tgt, ln in HOOK_VECTORS:
        want = patch[site:site + ln]
        got = b24(BASE + site, BASE + tgt, link=False)
        tag = 'b.w'
        _check(tag, site, tgt, want, got, bad)
    for site, tgt, ln in BL_VECTORS:
        want = patch[site:site + ln]
        got = b24(BASE + site, BASE + tgt, link=True)
        _check('bl.w', site, tgt, want, got, bad)
    if verbose:
        print(f'vectors={len(HOOK_VECTORS) + len(BL_VECTORS)} failures={len(bad)}')
        for b in bad:
            print('  MISMATCH', b)
    return not bad


def _check(tag, site, tgt, want, got, bad):
    ok = want == got
    line = f'  {tag:5s} f{site:07x} -> f{tgt:07x}  want={want.hex()} got={got.hex()} {"OK" if ok else "BAD"}'
    print(line)
    if not ok:
        bad.append((tag, site, tgt, want.hex(), got.hex()))


if __name__ == '__main__':
    import sys
    sys.exit(0 if validate() else 1)
