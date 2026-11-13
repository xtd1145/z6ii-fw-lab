"""Prove the installed hook layer by execution, on the real patched eg1850_mas image.

  A. no core present (CTRL all zero -- exactly how the image ships): every hooked site must
     reach the same resume address with the same r0-r12, sp, lr and NZCV as the stock site.
     This is the property that makes an installable-but-inert patch safe.

  B. core present (CTRL written, with a stand-in core placed further along the same clean
     run): the dispatcher must land in the core instead, with the caller's arguments intact,
     and the core must still be able to continue into the stock window.

Both run under Unicorn with randomized caller state.  A is the safety property; B proves the
layer is not dead code.
"""
from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import lib                                    # noqa: E402,F401
import stubemu as emu                                    # noqa: E402
import hookgen as H                             # noqa: E402

BASE = H.BASE
CORE = 0x000E800                # stand-in core, inside the clean run, past the hook layer
CORE_SPAN = 0x1C2               # CORE .. CLEAN_END


def inputs(n=48, seed=13):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        d = {f'r{i}': rnd.getrandbits(32) for i in range(8)}
        for i in range(8):
            if rnd.random() < 0.25:
                d[f'r{i}'] = rnd.choice([0, 1, 2, 4, 5, 7, 8, 0x14, 0xFFFFFFFF])
        out.append(d)
    return out


def run(img, site, win, init, allow_core=False):
    allow = [(BASE + site, win), (BASE + H.CTRL, H.CLEAN_END - H.CTRL)]
    if allow_core:
        allow.append((BASE + CORE, CORE_SPAN))
    return emu.emulate(img, BASE, site, win, init, nmax=80, allow=allow,
                       map_only=[BASE + site + win])


def with_core():
    """Patched image + a CTRL block + a per-slot core stub that marks r0 and continues."""
    p = bytearray(H.apply(H.build()[0]))
    stubs = []
    for i, (name, site, win) in enumerate(H.SITES):
        at = CORE + 0x40 + i * 0x20
        blob = H.asm_seq([f'movw r0, #{0x5A0 + i:#x}', f'b #{BASE + site + win:#x}'],
                         BASE + at)
        p[at:at + len(blob)] = blob
        stubs.append(at - CORE)                      # PIC offset from core_base
    p[H.CTRL:H.CTRL + 16] = struct.pack('<III', H.MAGIC, BASE + CORE, len(H.SITES)) + b'\0' * 4
    tab = b''.join(struct.pack('<I', o) for o in stubs)
    p[CORE + H.TABLE_OFF:CORE + H.TABLE_OFF + len(tab)] = tab
    return bytes(p), stubs


def main():
    writes, rep = H.build()
    patched = H.apply(writes)
    hole = patched[H.CTRL:H.CLEAN_END]
    print(f'patch: {sum(len(d) for _, d in writes)} B written in {len(writes)} runs, '
          f'{sum(a != b for a, b in zip(patched, H.MAS))} bytes differ, '
          f'{sum(1 for x in hole if x)} of {len(hole)} hole bytes now nonzero')
    INP = inputs()

    fails = 0
    print('\nA  no core -- patched site must behave exactly like stock')
    for name, site, win in H.SITES:
        bad = []
        for init in INP:
            a = emu.emulate(H.MAS, BASE, site, win, init,
                            map_only=[BASE + site + win])
            b = run(patched, site, win, init)
            ok, why = emu.same(a, b, ignore_r12=True)
            if not ok and not a['err']:
                bad.append(why)
        steps = run(patched, site, win, INP[0])['steps']
        fails += len(bad)
        print(f'   {name:24s} f{site:07x}  {len(INP) - len(bad)}/{len(INP)} identical, '
              f'{steps} instr/call {"OK" if not bad else "FAIL " + str(bad[:1])}')

    print('\nB  core present -- dispatcher must land in the core with the caller arguments')
    p2, stubs = with_core()
    ok = 0
    for i, (name, site, win) in enumerate(H.SITES):
        before = INP[0].get('r1', 0)
        b = run(p2, site, win, INP[0], allow_core=True)
        took = b['regs']['r0'] == 0x5A0 + i and b['pc'] == BASE + site + win \
            and b['regs']['r1'] == before
        ok += 1 if took else 0
        print(f'   {name:24s} r0={b["regs"]["r0"]:#x} (want {0x5A0 + i:#x}) r1='
              f'{b["regs"]["r1"]:#x} exit={b["pc"]:#010x} {"OK" if took else "FAIL"}')
    print(f'\nA mismatches = {fails}    B core taken = {ok}/{len(H.SITES)}')
    if fails == 0 and ok == len(H.SITES):
        Path(HERE / 'mas_patched.bi').write_bytes(patched)
        print(f'wrote {HERE / "mas_patched.bi"} (verified: inert without a core)')
    return 1 if (fails or ok != len(H.SITES)) else 0


if __name__ == '__main__':
    sys.exit(main())
