"""Layer 1 -- WL ground truth, then Layer 2 -- Z6 II self-equivalence.

L1: WonderLantern's shipped Z6 patch contains hand-written fallback stubs for real hook
    sites.  Emulating stock-window / WL-stub / my-stub from identical inputs must give
    identical post-state.  WL agreeing with stock proves the equivalence notion is the right
    one; my agreeing with both proves the engine reproduces the technique automatically.

L2: the same comparison for hundreds of real eg1850_mas code sites (no ground truth there,
    so my stub is compared against the stock window directly).
"""
from __future__ import annotations

import pickle
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
sys.path.insert(0, str(HERE))
import lib                                   # noqa: E402
import rearm as R                            # noqa: E402
import stubemu as emu                                   # noqa: E402

BASE = R.MAS_BASE
STOCK = lib.load(lib.Z6_STOCK)
PATCH = lib.load(lib.Z6_PATCH)
MAS = lib.load(lib.MAS)
STUB = 0x00F000            # file offset the relocated windows are built for

# site_off, stock window bytes, WL stub file offset, stub extent  (read back below, asserted)
WL = [
    ('switch-range', 0x03DF942, 4,  0x00E520, 8),
    ('cmp-branch',   0x03E5304, 4,  0x00E528, 14),
    ('two-cmp',      0x004B1AC, 8,  0x00E53C, 20),
]


def inputs(rnd, n=64, regs=('r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7')):
    out = []
    for _ in range(n):
        d = {r: rnd.getrandbits(32) for r in regs}
        for r in ('r0', 'r1', 'r4'):
            if rnd.random() < 0.3:
                d[r] = 0xC0000000 | rnd.getrandbits(20)      # plausible firmware pointer
            if rnd.random() < 0.2:
                d[r] = rnd.choice([0, 1, 4, 5, 7, 8, 0xFFFFFFFF])
        d['nzcv'] = rnd.getrandbits(4)
        out.append(d)
    return out


def slots(img, off, win, base=BASE):
    """Literal-slot VMAs referenced by a window: the stub must be able to read them."""
    out = []
    for i in R.decode_run(img, off, win, base):
        out += [(v, img, base) for k, v in R.pc_addr(i) if k in ('literal', 'adr')]
    return out


def trial(img_a, off_a, n_a, img_b, off_b, n_b, init):
    a = emu.emulate(img_a, BASE, off_a, n_a, init)
    b = emu.emulate(img_b, BASE, off_b, n_b, init)
    return a, b, emu.same(a, b)


def l1():
    rnd = random.Random(7)
    bad = 0
    print('== L1  WL ground truth (Z6 eg1620) ==')
    for tag, site, win, stub, stubn in WL:
        stock_bytes = bytes(STOCK[site:site + win])
        IN = inputs(rnd, 40)
        a = [emu.emulate(STOCK, BASE, site, win, i) for i in IN]
        wl = [emu.emulate(PATCH, BASE, stub, stubn, i, extra=[(BASE + site + win, PATCH, BASE)])
         for i in IN]
        live = [(x, y) for x, y in zip(a, wl) if not x['err']]
        n_wl = sum(1 for x, y in live if emu.same(x, y)[0])
        blob, log = R.rearm(STOCK, site, win, STUB, site + win)
        my = [emu.emulate(blob, BASE + STUB, 0, len(blob), i,
                  extra=[(BASE + site + win, STOCK, BASE)] + slots(STOCK, site, win))
        for i in IN]
        n_my = sum(1 for x, y in zip(a, my) if not x['err'] and emu.same(x, y)[0])
        ok = n_wl == n_my == len(live) and len(live) > 0
        bad += 0 if ok else 1
        print(f'  {tag:12s} site f{site:07x} win={win}B {stock_bytes.hex()}  '
              f'WL={n_wl}/{len(live)}  mine={n_my}/{len(live)}  stub={len(blob)}B  '
              f'{"OK" if ok else "FAIL"}')
        if not ok:
            for x, y in zip(a, my):
                if not x['err'] and not emu.same(x, y)[0]:
                    print(f'      mine vs stock: {emu.same(x, y)[1]}')
                    print('      ' + emu.fmt(x) + '\n      ' + emu.fmt(y))
                    break
            for line in log:
                print('      ' + line)
    return bad


def l2(nsite=250, ninput=24):
    print('\n== L2  Z6 II eg1850_mas self-equivalence ==')
    br = pickle.load(open(str(HERE.parent / 'z6ii_mem' / 'cache' / 'br_mas.pkl'), 'rb'))
    tgt, kind = br['tgt'], br['kind']
    entries = sorted({int(t) for t, k in zip(tgt, kind) if k == 1 and 0 < t < len(MAS) - 0x60})
    rnd = random.Random(20261026)
    stats = {'pass': 0, 'fail': 0, 'refuse': 0, 'skip': 0}
    fails = []
    sites = []
    for off in rnd.sample(entries, min(nsite, len(entries))):
        sites.append((off, off))                       # entry: the anchor is the entry itself
        pos = off
        for x in R._cs().disasm(bytes(MAS[off:off + 0x40]), BASE + off):
            pos += x.size
            if pos > off + 4:
                sites.append((pos, off))               # mid-function, proven from that entry
                break
    for off, anchor in sites:
        for win in (4, 8):
            try:
                blob, log = R.rearm(MAS, off, win, STUB, off + win, anchor=anchor)
            except Exception:
                stats['refuse'] += 1
                continue
            IN = inputs(rnd, ninput)
            a = [emu.emulate(MAS, BASE, off, win, i) for i in IN]
            b = [emu.emulate(blob, BASE + STUB, 0, len(blob), i,
                  extra=[(BASE + off + win, MAS, BASE)] + slots(MAS, off, win)) for i in IN]
            live = [(x, y) for x, y in zip(a, b) if not x['err']]
            if not live:
                stats['skip'] += 1
                continue
            good = sum(1 for x, y in live if emu.same(x, y)[0])
            if good == len(live):
                stats['pass'] += 1
            else:
                stats['fail'] += 1
                if len(fails) < 6:
                    x, y = next((x, y) for x, y in live if not emu.same(x, y)[0])
                    fails.append((off, win, log, emu.same(x, y)[1], x, y))
    print('  ' + '  '.join(f'{k}={v}' for k, v in stats.items()))
    for off, win, log, why, x, y in fails:
        print(f'  FAIL f{off:#07x} win={win}: {why}')
        for l in log:
            print('       ' + l)
        print('       stock ' + emu.fmt(x))
        print('       mine  ' + emu.fmt(y))
    return 1 if stats['fail'] else 0


if __name__ == '__main__':
    sys.exit(l1() + l2())
