"""Unicorn harness: execute a Thumb instruction window and report its post-state.

emulate(img, base, off, nbytes, init) runs the window at file offset `off` (vma = off + base),
staying inside [off, off+nbytes); it stops the instant the PC leaves that region and reports
(exit_vma, r0..r12, sp, lr, N/Z/C/V).  Two windows are equivalent when, from identical initial
state, they report identical tuples -- which is exactly the claim the re-arm engine makes.

DRAM and a stack are mapped zero-filled so literal derefs succeed without modelling firmware.
"""
from __future__ import annotations

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_PROT_ALL, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_R0, UC_ARM_REG_XPSR)

DRAM, DRAM_N = 0xC0000000, 0x06000000
STACK, STACK_N = 0x90000000, 0x00100000
SPAN = 0x00010000


def _regs():
    return [UC_ARM_REG_R0 + i for i in range(13)] + [UC_ARM_REG_SP, UC_ARM_REG_LR]


REGIDS = _regs()
NAMES = [f'r{i}' for i in range(13)] + ['sp', 'lr']


def emulate(img: bytes, base: int, off: int, nbytes: int, init: dict, nmax: int = 40,
            extra=(), allow=None, map_only=()):
    """Run from `off`. Execution is "ours" while the PC is inside any region of `allow`
    (default: just the window); the first PC outside all of them is reported as the exit."""
    lo = off + base
    regions = list(allow) if allow else [(lo, nbytes)]
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    mu.mem_map(DRAM, DRAM_N, UC_PROT_ALL)
    mu.mem_map(STACK, STACK_N, UC_PROT_ALL)

    def fill(src, sbase, vma_start, vma_end):
        vlo = vma_start & ~(SPAN - 1)
        vend = (vma_end + SPAN - 1) & ~(SPAN - 1)
        for a in range(vlo, vend, SPAN):
            try:
                mu.mem_map(a, SPAN, UC_PROT_ALL)
            except Exception:
                pass                            # already mapped by the window itself
            f = a - sbase
            s, e = max(0, f), min(len(src), f + SPAN)
            if e > s:
                mu.mem_write(sbase + s, bytes(src[s:e]))

    for a, n in regions:
        fill(img, base, a - SPAN, a + n + SPAN)
    for vma, src, sbase in extra:               # resume targets live in the real image
        fill(src, sbase, vma, vma + 2)
    for vma in map_only:                        # never executed, but must be fetchable
        a = vma & ~(SPAN - 1)
        try:
            mu.mem_map(a, SPAN, UC_PROT_ALL)
        except Exception:
            pass
    mu.reg_write(UC_ARM_REG_SP, init.get('sp', STACK + STACK_N - 0x400))
    mu.reg_write(UC_ARM_REG_LR, init.get('lr', 0x0A000001))
    for i in range(13):
        if f'r{i}' in init:
            mu.reg_write(UC_ARM_REG_R0 + i, init[f'r{i}'])
    mu.reg_write(UC_ARM_REG_XPSR, (init.get('nzcv', 0) & 0xF) << 28 | 0x60000000)
    seen = {'n': 0, 'out': None}

    def inside(addr):
        return any(a <= addr < a + n for a, n in regions)

    def hook(u, addr, size, ud):
        if not inside(addr):
            seen['out'] = addr
            u.emu_stop()
            return
        seen['n'] += 1

    hh = mu.hook_add(UC_HOOK_CODE, hook)
    err = None
    try:
        mu.emu_start(lo | 1, 0, count=nmax)
    except Exception as e:                       # unMapped fetch, invalid instr, cp error ...
        err = f'{type(e).__name__}: {e}'
    mu.hook_del(hh)
    pc = seen['out'] if seen['out'] is not None else mu.reg_read(UC_ARM_REG_PC)
    vals = [mu.reg_read(r) for r in REGIDS]
    flags = (mu.reg_read(UC_ARM_REG_XPSR) >> 28) & 0xF
    return {'pc': pc, 'regs': dict(zip(NAMES, vals)), 'flags': flags,
            'steps': seen['n'], 'err': err}


def same(a, b, ignore_lr=False, ignore_sp=False, ignore_r12=False):
    """(bool, detail) -- are two post-states identical?"""
    d = []
    if a['err'] or b['err']:
        d.append(f"err {a['err']} | {b['err']}")
    if a['pc'] != b['pc']:
        d.append(f"exit pc {a['pc']:#x} != {b['pc']:#x}")
    for n in NAMES:
        if ignore_lr and n == 'lr':
            continue
        if ignore_sp and n == 'sp':
            continue
        if ignore_r12 and n == 'r12':
            continue
        if a['regs'][n] != b['regs'][n]:
            d.append(f'{n} {a["regs"][n]:#x} != {b["regs"][n]:#x}')
    if a['flags'] != b['flags']:
        d.append(f'flags {a["flags"]:x} != {b["flags"]:x}')
    return (not d), '; '.join(d) or 'identical'


def fmt(st):
    r = ' '.join(f'{n}={st["regs"][n]:#x}' for n in NAMES if n not in ('sp', 'lr'))
    return (f'pc={st["pc"]:#010x} flags={st["flags"]:x} sp={st["regs"]["sp"]:#x} '
            f'lr={st["regs"]["lr"]:#x} {r}')
