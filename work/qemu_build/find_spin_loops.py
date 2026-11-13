#!/usr/bin/env python3
"""Derive, from the firmware itself, what every MMIO polling loop must read.

mas waits on status bits in peripherals we have no specification for. Each wait
is a short backward branch, so the value that terminates it is recoverable from
the branch condition plus the test feeding it. That list is what the QEMU machine
has to satisfy to get past init.

 1. Every beq/bne that branches back no more than 12 instructions is a spin loop.
 2. Resolve register bases two ways, because the compiler emits both:
      - a "ldr Rt, [pc, #literal]" inside the loop body, and
      - one in the 64 instructions *before* the loop (the 0x1002c2a0 wait loads
        its base outside and only tests inside).
    A literal counts as a register base when it is 4-byte aligned and inside
    0x04000000..0xf0000000, the range the module's own page table calls device.
 3. The body's load through such a base gives the polled address (plus its
    offset); the test that feeds the branch gives the value needed to leave:
      tst rX, #mask / beq   loops while those bits are clear -> they must be SET
      tst rX, #mask / bne   loops while those bits are set   -> they must be CLEAR
      cmp rX, #v    / bne   loops while != v                 -> must EQUAL v
      cmp rX, #v    / beq   loops while == v                 -> must differ from v
    "cmp rX, rY" folds through a "mov rY, #const" in the same body, which is how
    the 40 engine handshakes are written.
 4. A word with contradictory constraints is reported as a conflict, not silently
    resolved.

Usage: python find_spin_loops.py [blob] [load-va-in-hex]
"""
import struct, sys

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700"
base = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x10020000

blob = open(path, "rb").read()
n = len(blob) // 4
W = struct.unpack_from("<%dI" % n, blob, 0)
MMIO_LO, MMIO_HI = 0x04000000, 0xF0000000


def f(x, hi, lo):
    return (x >> lo) & ((1 << (hi - lo + 1)) - 1)


def sx(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def rot_imm(x):
    val, rot = x & 0xFF, (x >> 8) & 0xF
    return ((val >> (2 * rot)) | (val << ((32 - 2 * rot) % 32))) & 0xFFFFFFFF if rot \
        else val


def is_ldr(x):
    """ldr Rt, [Rn, #imm] (any condition but NEVER): bits[27:20] == 0x59."""
    return f(x, 31, 28) != 0xF and f(x, 27, 20) == 0x59


def literal(i):
    """Value of a pc-relative literal load at word index i, or None."""
    x = W[i]
    if not is_ldr(x) or f(x, 19, 16) != 15:
        return None
    pool = i * 4 + 8 + f(x, 11, 0)
    if pool % 4 or pool + 4 > len(blob):
        return None
    return struct.unpack_from("<I", blob, pool)[0]


def tst_cmp(x):
    """(opcode, Rn, operand2 value) for TST/CMP, else None."""
    if f(x, 31, 28) == 0xF or f(x, 27, 26) != 0 or f(x, 20, 20) != 1 \
            or f(x, 15, 12) != 0:
        return None
    op = f(x, 24, 21)
    if op not in (0x8, 0xA):
        return None
    if f(x, 25, 25):
        return op, f(x, 19, 16), rot_imm(x)
    return op, f(x, 19, 16), ("rm", f(x, 3, 0))


def mov_imm(x):
    """Register written by "MOV Rd, #const", and its value, else (None, None)."""
    if f(x, 31, 28) == 0xF or f(x, 27, 20) != 0x3A or f(x, 19, 16) != 0:
        return None, None
    return f(x, 15, 12), rot_imm(x)


need = {}
for i in range(n - 1):
    x = W[i]
    cond = f(x, 31, 28)
    if cond not in (0, 1) or f(x, 27, 25) != 0b101:
        continue
    t = i + 2 + sx(f(x, 23, 0), 24)
    if not (0 <= t < i and i - t <= 12):
        continue

    bases = {}
    for j in range(max(0, t - 64), t):      # base loaded before the loop
        reg, val = mov_imm(W[j])
        lit = literal(j)
        if is_ldr(W[j]) and f(W[j], 19, 16) == 15 and lit is not None \
                and MMIO_LO <= lit < MMIO_HI:
            bases[f(W[j], 15, 12)] = lit
    loaded, movs, test = {}, {}, None
    for j in range(t, i + 1):
        y = W[j]
        lit = literal(j)
        if lit is not None:                                   # base inside the loop
            if MMIO_LO <= lit < MMIO_HI:
                bases[f(y, 15, 12)] = lit
            continue
        if is_ldr(y) and f(y, 19, 16) in bases:
            loaded[f(y, 15, 12)] = bases[f(y, 19, 16)] + f(y, 11, 0)
            continue
        reg, val = mov_imm(y)
        if reg is not None:
            movs[reg] = val
            continue
        d = tst_cmp(y)
        if d:
            op, rn, op2 = d
            if isinstance(op2, tuple):
                op2 = movs.get(op2[1])
            if op2 is not None and rn in loaded:
                test = (op, rn, op2)

    if test is None:
        continue
    op, rn, val = test
    addr = loaded[rn]
    slot = need.setdefault(addr, {"set": 0, "clear": 0, "eq": set(),
                                  "ne": set(), "loops": set()})
    slot["loops"].add(base + t * 4)
    if op == 0x8:                                   # TST
        slot["set" if cond == 0 else "clear"] |= val
    else:                                           # CMP
        slot["eq" if cond == 1 else "ne"].add(val)

print("%s: %d MMIO words are polled by a spin loop" % (path.split("\\")[-1], len(need)))
for a in sorted(need):
    s = need[a]
    loops = " ".join(hex(l) for l in sorted(s["loops"])[:4])
    bad = bool(s["set"] & s["clear"]) or bool(s["eq"] & s["ne"]) or \
          any(v & s["clear"] for v in s["eq"])
    want = s["set"]
    for v in s["eq"]:
        want |= v
    want &= ~s["clear"]
    if not want:
        want = 0 if 0 not in s["ne"] else 1
    print("  0x%08x  read 0x%08x  set=0x%x clear=0x%x eq=%s ne=%s  n=%d  @%s%s"
          % (a, want, s["set"], s["clear"], sorted(hex(v) for v in s["eq"]),
             sorted(hex(v) for v in s["ne"]), len(s["loops"]), loops,
             "  <== CONFLICT" if bad else ""))
