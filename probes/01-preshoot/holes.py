"""Which parts of the eg1850_mas zero holes are provably untouched?

A zero run is only usable as code space if nothing reads or writes it.  Two independent
inbound-reference kinds are counted per 4 KiB bucket:

  dword   an aligned word anywhere in the module whose value falls inside the hole
          (a pointer, a vtable entry, or a return address -- whoever created it, some code
          is meant to land there)
  branch  a bl/b target inside the hole, from the module's own branch record

A bucket with both counts at zero has no evidence of use, so writing code there cannot
destroy data.  Anything else stays untouched.  This is deliberately conservative: the
"branch" record was produced by linear disassembly, so data that merely looks like code
still counts as a reference against our use of the space.

Read-only.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'z6ii_mem'))
import lib                                     # noqa: E402

MAS = lib.load(lib.MAS)
BASE = lib.MAS_BASE
BUCK = 0x1000
HOLE_MIN = 0x4000


def zero_runs(b, minlen=HOLE_MIN):
    arr = np.frombuffer(memoryview(b), dtype=np.uint8)
    z = (arr == 0).astype(np.int8)
    d = np.diff(np.concatenate(([0], z, [0])))
    st, en = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    return [(int(s), int(e) - int(s)) for s, e in zip(st, en) if e - s >= minlen]


def inbound(b, hs, he, br):
    """Return (dword_targets, branch_targets) as index arrays into the hole."""
    w = lib.words(b)
    lo, hi = BASE + hs, BASE + he
    m = (w >= lo) & (w < hi)
    # a reference counts only if it lives outside the hole itself
    src = np.nonzero(m)[0] * 4
    keep = (src < hs) | (src >= he)
    dw = w[m][keep]                                # the pointed-to VMAs
    t = np.asarray(br['tgt'])
    k = np.asarray(br['kind'])
    mb = (t >= hs) & (t < he)
    srcb = np.asarray(br['src'])
    kb = np.nonzero(mb & ((srcb < hs) | (srcb >= he)))[0]
    return dw, t[kb], k[kb]


def main():
    br = pickle.load(open(str(Path(__file__).resolve().parents[1] /
                          'z6ii_mem' / 'cache' / 'br_mas.pkl'), 'rb'))
    runs = sorted(zero_runs(MAS), key=lambda r: -r[1])
    print(f'mas 0x{len(MAS):x} bytes, {len(runs)} zero runs >= 0x{HOLE_MIN:x}')
    print(f'{"hole":24s} {"len":9s} {"dword":7s} {"branch":7s}  clean sub-runs (>=0x800)')
    for hs, ln in runs[:12]:
        he = hs + ln
        dw, bt, bk = inbound(MAS, hs, he, br)
        nb = (ln + BUCK - 1) // BUCK
        cd = np.bincount((dw - (BASE + hs)) // BUCK, minlength=nb + 1)[:nb]
        cb = np.bincount((bt - hs) // BUCK, minlength=nb + 1)[:nb]
        clean = (cd == 0) & (cb == 0)
        # merge consecutive clean buckets into sub-runs
        runs_ok, s = [], None
        for i in range(nb):
            if clean[i] and s is None:
                s = i
            if (not clean[i] or i == nb - 1) and s is not None:
                e = (i + 1) if (clean[i] and i == nb - 1) else i
                if (e - s) * BUCK >= 0x800:
                    runs_ok.append((hs + s * BUCK, (e - s) * BUCK))
                s = None
        runs_ok.sort(key=lambda r: -r[1])
        top = '  '.join(f'f{a:07x}+0x{b:x}' for a, b in runs_ok[:3]) or 'none'
        print(f'f{hs:07x}..f{he:07x}  0x{ln:<8x} {len(dw):<7d} {len(bt):<7d}  {top}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
