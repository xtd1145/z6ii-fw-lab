"""A/B execution proof for the pre-shoot core, on the real patched eg1850_mas image.

A  no core  (CTRL zeroed -- the "uninstalled" configuration)
   Every hooked site must reach the same resume address with the same r0-r11, sp, lr and
   NZCV as stock.  This is the property that keeps an installed patch safe if the core is
   ever disabled.

B  core present (the shipped image, CTRL already installed)
   B1  ABI: same equality as A, because the core saves and restores everything it touches.
   B2  Behaviour: the state machine really runs --
         · STATUS.magic becomes 'Z6PS', armed = 1
         · each call appends {slot, arg0, arg1, lr} to the descriptor ring
         · a call through the trigger slot freezes the ring, sets retained = min(count, N)
           and stops appending
         · the ring wraps at N without losing the count

Unlike test_hooks.py this keeps ONE emulator per image so STATUS/RING accumulate across
calls, which is what the firmware will see.

Read-only: both images are opened from the repo, never written.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import lib                                     # noqa: E402,F401
import hookgen as H                             # noqa: E402
from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_PROT_ALL, UC_HOOK_CODE)  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,     # noqa: E402
                               UC_ARM_REG_R0, UC_ARM_REG_XPSR)

BASE = H.BASE
DRAM, DRAM_N = 0xC0000000, 0x06000000
STACK, STACK_N = 0x90000000, 0x00100000
REGIDS = [UC_ARM_REG_R0 + i for i in range(13)] + [UC_ARM_REG_SP, UC_ARM_REG_LR]
NAMES = [f'r{i}' for i in range(13)] + ['sp', 'lr']
ST_FIELDS = ('magic', 'count', 'write_idx', 'frozen', 'retained', 'triggers', 'armed',
             'magic_inv', 'jpeg_hits', 'jpeg_slot', 'jpeg_addr', 'page', 'save_actions',
             'np3_writes', 'np3_addr', 'rsv0', 'rsv1', 'rsv2', 'rsv3', 'rsv4', 'rsv5',
             'rsv6', 'rsv7', 'rsv8')


class Runner:
    """One emulated machine: the image is loaded once so STATUS/RING persist across calls."""

    def __init__(self, img):
        self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.mu.mem_map(DRAM, DRAM_N, UC_PROT_ALL)
        self.mu.mem_map(STACK, STACK_N, UC_PROT_ALL)
        self.mu.mem_map(BASE, 0x2500000, UC_PROT_ALL)
        self.mu.mem_write(BASE, bytes(img))
        self.allow = [(BASE + H.CTRL, H.CLEAN_END - H.CTRL)]

    def call(self, site, win, init, nmax=3000):
        mu = self.mu
        regions = self.allow + [(BASE + site, win)]
        mu.reg_write(UC_ARM_REG_SP, init.get('sp', STACK + STACK_N - 0x400))
        mu.reg_write(UC_ARM_REG_LR, init.get('lr', 0x0A000001))
        for i in range(13):
            mu.reg_write(UC_ARM_REG_R0 + i, init.get(f'r{i}', 0))
        mu.reg_write(UC_ARM_REG_XPSR, (init.get('nzcv', 0) & 0xF) << 28 | 0x60000000)
        out = {'pc': None, 'err': None}

        def inside(a):
            return any(lo <= a < lo + n for lo, n in regions)

        def hook(u, addr, size, ud):
            if not inside(addr):
                out['pc'] = addr
                u.emu_stop()

        hh = mu.hook_add(UC_HOOK_CODE, hook)
        try:
            mu.emu_start((BASE + site) | 1, 0, count=nmax)
        except Exception as e:
            out['err'] = f'{type(e).__name__}: {e}'
        mu.hook_del(hh)
        pc = out['pc'] if out['pc'] is not None else mu.reg_read(UC_ARM_REG_PC)
        regs = dict(zip(NAMES, [mu.reg_read(r) for r in REGIDS]))
        flags = (mu.reg_read(UC_ARM_REG_XPSR) >> 28) & 0xF
        return {'pc': pc, 'regs': regs, 'flags': flags, 'err': out['err']}

    def status(self):
        raw = bytes(self.mu.mem_read(H.STATUS, 96))
        return dict(zip(ST_FIELDS, struct.unpack('<24I', raw)))

    def ring(self, i):
        raw = bytes(self.mu.mem_read(H.RING + i * 16, 16))
        return struct.unpack('<4I', raw)


def states(n=48, seed=13):
    import random
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        d = {f'r{i}': rnd.getrandbits(32) for i in range(8)}
        for i in range(8):
            if rnd.random() < 0.25:
                d[f'r{i}'] = rnd.choice([0, 1, 2, 4, 5, 7, 8, 0x14, 0xFFFFFFFF])
        d['lr'] = rnd.getrandbits(32) | 1
        d['nzcv'] = rnd.getrandbits(4)
        out.append(d)
    return out


def same_regs(a, b):
    d = []
    if a['err'] or b['err']:
        d.append(f"err {a['err']} | {b['err']}")
    if a['pc'] != b['pc']:
        d.append(f"exit {a['pc']:#x} != {b['pc']:#x}")
    for n in NAMES:
        if n == 'r12':
            continue                       # dispatcher hand-over, AAPCS scratch at an entry
        if a['regs'][n] != b['regs'][n]:
            d.append(f'{n} {a["regs"][n]:#x} != {b["regs"][n]:#x}')
    if a['flags'] != b['flags']:
        d.append(f'flags {a["flags"]:x} != {b["flags"]:x}')
    return (not d), '; '.join(d) or 'identical'


def main():
    ok = True
    writes, rep = H.build()
    patched = H.apply(writes)
    nocore = bytearray(patched)
    nocore[H.CTRL:H.CTRL + 16] = b'\0' * 16
    print(f'image: {sum(a != b for a, b in zip(patched, H.MAS))} bytes differ from stock, '
          f'core installed at {BASE + H.CORESLOT:#x}')

    states_ = states()
    stock = Runner(H.MAS)

    # ---- A: core removed ------------------------------------------------------
    print('\nA  no core (CTRL zeroed) -- each site must equal stock')
    nz = Runner(bytes(nocore))
    a_fail = 0
    for name, site, win in H.SITES:
        bad = 0
        for init in states_:
            ra = stock.call(site, win, init)
            rb = nz.call(site, win, init)
            good, why = same_regs(ra, rb)
            if not good and not ra['err']:
                bad += 1
        a_fail += bad
        print(f'   {name:24s} {len(states_) - bad}/{len(states_)} identical'
              f'{"  OK" if not bad else "  FAIL"}')
    ok &= a_fail == 0
    st = nz.status()
    print(f'   state untouched while no core is installed: magic={st["magic"]:#x} '
          f'count={st["count"]}')
    ok &= st['magic'] == 0 and st['count'] == 0

    # ---- B1: ABI with the core live -------------------------------------------
    print('\nB1 core present -- same equality, because the core restores everything')
    live = Runner(patched)
    b_fail = 0
    for name, site, win in H.SITES:
        bad = 0
        for init in states_:
            ra = stock.call(site, win, init)
            rb = live.call(site, win, init)
            good, why = same_regs(ra, rb)
            if not good and not ra['err']:
                bad += 1
                if bad == 1:
                    print(f'      first mismatch at {name}: {why}')
        b_fail += bad
        print(f'   {name:24s} {len(states_) - bad}/{len(states_)} identical'
              f'{"  OK" if not bad else "  FAIL"}')
    ok &= b_fail == 0

    # ---- B2: behaviour --------------------------------------------------------
    print('\nB2 pre-shoot state machine')
    r = Runner(patched)
    n = len(H.SITES)
    TRIG = H.TRIGGER_SLOT
    REC = [k for k in range(n) if k not in (TRIG, H.IO_SLOT)]
    inputs = []
    for k in REC:                                # recording slots only
        init = {'r0': 0xA000 + k, 'r1': 0xB000 + k, 'lr': 0xC000 + k, 'nzcv': k & 0xF}
        inputs.append(init)
        res = r.call(H.SITES[k][1], H.SITES[k][2], init)
        if res['err']:
            print(f'   slot {k} error: {res["err"]}')
            ok = False
    st = r.status()
    print(f'   after {len(REC)} calls: magic={st["magic"]:#x} ({b"Z6PS".decode() if st["magic"] == H.SMAGIC else "?"}), '
          f'armed={st["armed"]} count={st["count"]} frozen={st["frozen"]}')
    ok &= st['magic'] == H.SMAGIC and st['armed'] == 1 and st['count'] == len(REC) \
        and st['frozen'] == 0
    ring_ok = True
    for k in REC:
        slot, a0, a1, lr = r.ring(k)
        if (slot, a0, a1, lr) != (k, 0xA000 + k, 0xB000 + k, 0xC000 + k):
            ring_ok = False
            print(f'   ring[{k}] = {{slot={slot}, r0={a0:#x}, r1={a1:#x}, lr={lr:#x}}} '
                  f'expected {{slot={k}, r0={0xA000+k:#x}, r1={0xB000+k:#x}, lr={0xC000+k:#x}}}')
    print(f'   descriptor ring holds exactly the hook arguments: '
          f'{"OK" if ring_ok else "FAIL"}')
    ok &= ring_ok

    trig_win = H.SITES[TRIG][2]
    r.call(H.SITES[TRIG][1], trig_win, {'r0': 0xDEAD, 'r1': 0xBEEF, 'lr': 0xD000})
    st = r.status()
    want_ret = min(len(REC), H.NSLOT)
    print(f'   after the trigger slot: frozen={st["frozen"]} retained={st["retained"]} '
          f'triggers={st["triggers"]} count={st["count"]} (want frozen=1, '
          f'retained={want_ret}, triggers=1, count={len(REC)})')
    ok &= st['frozen'] == 1 and st['retained'] == want_ret and st['triggers'] == 1 \
        and st['count'] == len(REC)
    r.call(H.SITES[0][1], H.SITES[0][2], {'r0': 0x1111})
    st2 = r.status()
    print(f'   after a further call while frozen: count={st2["count"]} '
          f'(want {len(REC)}, appended nothing)')
    ok &= st2['count'] == len(REC)

    # ring wrap: fresh machine, many calls
    w = Runner(patched)
    for k in range(H.NSLOT + 12):
        w.call(H.SITES[0][1], H.SITES[0][2], {'r0': 0xE000 + k})
    st = w.status()
    last_idx = (H.NSLOT + 12 - 1) % H.NSLOT
    slot, a0, a1, lr = w.ring(last_idx)
    wrap_ok = (st['count'] == H.NSLOT + 12 and st['retained'] == H.NSLOT
               and st['write_idx'] == (H.NSLOT + 12) % H.NSLOT and a0 == 0xE000 + H.NSLOT + 11)
    print(f'   wrap after {H.NSLOT + 12} calls: count={st["count"]} retained={st["retained"]} '
          f'write_idx={st["write_idx"]} last descriptor r0={a0:#x} '
          f'{"OK" if wrap_ok else "FAIL"}')
    ok &= wrap_ok

    # ---- B3: the state block lives in DRAM, so it must tolerate junk -----------------
    import random as _r
    rnd = _r.Random(5)
    for label, pre in (('all-ones', b'\xff' * 32),
                       ('random', bytes(rnd.getrandbits(8) for _ in range(32))),
                       ('magic-only', struct.pack('<I', H.SMAGIC) + b'\x00' * 28)):
        g = Runner(patched)
        g.mu.mem_write(H.STATUS, pre)
        g.call(H.SITES[0][1], H.SITES[0][2], {'r0': 0x1234})
        st = g.status()
        good = (st['magic'] == H.SMAGIC and st['count'] == 1 and st['armed'] == 1
                and g.ring(0)[1] == 0x1234)
        print(f'   B3 pre-filled {label:11s} -> magic={st["magic"]:#x} count={st["count"]} '
              f'armed={st["armed"]} {"OK" if good else "FAIL"}')
        ok &= good

    # ---- B4: the JPEG probe actually recognises a bitstream -------------------------
    JP = 0xC5E00000                       # inside the probe window, inside mapped DRAM
    jpeg = bytes.fromhex('ffd8ffe000104a46494600010100000100010000')
    junk = bytes.fromhex('1122334455667788')
    cases = [
        ('r0 -> JPEG',     3, {'r0': JP, 'r1': 0},              True,  1, 3, JP),
        ('r1 -> JPEG',     4, {'r0': 1, 'r1': JP + 0x100},      True,  2, 3, JP),
        ('r0 -> junk',     5, {'r0': JP + 0x200},               False, 2, 3, JP),
        ('r0 outside win', 6, {'r0': 0x90000000},               False, 2, 3, JP),
        ('r0 unaligned',   6, {'r0': JP + 0x201},               False, 2, 3, JP),
    ]
    pr = Runner(patched)
    pr.mu.mem_write(JP, jpeg)
    pr.mu.mem_write(JP + 0x100, jpeg)
    pr.mu.mem_write(JP + 0x200, junk)
    for label, slot, init, hit, want_hits, want_slot, want_addr in cases:
        site, win = H.SITES[slot][1], H.SITES[slot][2]
        res = pr.call(site, win, init)
        st = pr.status()
        good = (res['err'] is None and st['jpeg_hits'] == want_hits
                and st['jpeg_slot'] == want_slot and st['jpeg_addr'] == want_addr)
        print(f'   B4 {label:16s} hits={st["jpeg_hits"]} slot={st["jpeg_slot"]} '
              f'addr={st["jpeg_addr"]:#x} {"OK" if good else "FAIL"}')
        ok &= good

    # ---- B5: card write-API chunk channel ------------------------------------------
    # The camera funnels every picture-control save through the card write API as a
    # 0xF8-byte NCP buffer.  For PICCON96..99 the hook replaces that buffer with one
    # 248-byte chunk of probe data.  Four saves must reassemble the full 864 B.
    np = Runner(patched)
    STREAM, PATH, DESC, BUF = 0xC5E00000, 0xC5E00100, 0xC5E00200, 0xC5E00300
    np.mu.mem_write(STREAM, struct.pack('<I', PATH))
    np.mu.mem_write(DESC, b'\0' * 8 + struct.pack('<II', BUF, 0x350))

    def write_chunk(slot, size=0xF8, magic=b'NCP\0', prep=True):
        path = (r'B:\NIKON\CUSTOMPC\PICCON%d.NP3' % slot).encode() + b'\0'
        np.mu.mem_write(PATH, path)
        if prep:
            np.mu.mem_write(BUF, magic + bytes(0x200 - len(magic)))
        res = np.call(H.SITES[H.IO_SLOT][1], H.SITES[H.IO_SLOT][2],
                      {'r0': STREAM, 'r1': DESC, 'r2': size}, nmax=8000)
        return res, bytes(np.mu.mem_read(BUF, 0x100))

    chunks = {}
    hdr_ok = True
    for i in range(4):
        res, raw = write_chunk(96 + i)
        magic32, = struct.unpack_from('<I', raw, 0)
        ch, total = raw[4], raw[5]
        ln, = struct.unpack_from('<H', raw, 6)
        off, = struct.unpack_from('<I', raw, 8)
        s16, ver = struct.unpack_from('<HH', raw, 0xc)
        data = raw[16:16 + ln]
        want = sum(int.from_bytes(data[j:j + 2], 'little') for j in range(0, len(data), 2)) & 0xFFFF
        good = (magic32 == H.CHUNK_MAGIC and ch == i and total == H.CHUNK_TOTAL
                and ln == (H.CHUNK_DATA if i < 3 else H.CHUNK_LAST)
                and off == i * H.CHUNK_DATA and ver == 1 and s16 == want
                and res['err'] is None)
        if not good:
            print(f'   B5 chunk {i} BAD: magic={magic32:#x} ch={ch} total={total} len={ln} '
                  f'off={off} sum={s16:#06x} ver={ver} err={res["err"]}')
        hdr_ok &= good
        chunks[i] = (off, data)
    st = np.status()
    assembled = bytearray(H.CHUNK_LEN)
    for off, data in chunks.values():
        assembled[off:off + len(data)] = data
    src = bytearray(np.mu.mem_read(H.CHUNK_SRC, H.CHUNK_LEN))
    # chunk 0 is copied before its own np3_writes/np3_addr update, so those two
    # diagnostic fields in the page are stale by definition; everything else must match.
    for off in (0x34, 0x38):
        src[off:off + 4] = assembled[off:off + 4]
    print(f'   B5 chunks 96..99: headers {"OK" if hdr_ok else "FAIL"}, '
          f'assembled==STATUS+RING {"OK" if bytes(assembled) == bytes(src) else "FAIL"}, '
          f'writes={st["np3_writes"]} addr={st["np3_addr"]:#x}')
    ok &= hdr_ok and bytes(assembled) == bytes(src) and st['np3_writes'] == 4 \
        and st['np3_addr'] == BUF

    # gates: wrong slot, wrong size, wrong buffer magic must leave the buffer untouched
    gate_ok = True
    for label, kwargs in (('slot 01', dict(slot=1)),
                          ('size 0x342', dict(slot=96, size=0x342)),
                          ('no NCP magic', dict(slot=96, magic=b'JPEG'))):
        res, raw = write_chunk(**kwargs)
        magic = kwargs.get('magic', b'NCP\0')
        untouched = raw[:0x100] == magic + bytes(0x100 - len(magic))
        if res['err'] is not None or not untouched:
            print(f'   B5 gate {label} FAIL (untouched={untouched} err={res["err"]})')
            gate_ok = False
    st2 = np.status()
    print(f'   B5 gates (slot/size/magic) {"OK" if gate_ok else "FAIL"}, '
          f'writes={st2["np3_writes"]}')
    ok &= gate_ok and st2['np3_writes'] == 4

    print(f'\nRESULT  A {"pass" if a_fail == 0 else "FAIL"}  '
          f'B1 {"pass" if b_fail == 0 else "FAIL"}  '
          f'B2 {"pass" if ok else "FAIL"}')
    if ok:
        Path(HERE / 'mas_patched.bi').write_bytes(patched)
        print(f'wrote {HERE / "mas_patched.bi"}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
