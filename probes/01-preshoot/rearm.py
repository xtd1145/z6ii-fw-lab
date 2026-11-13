"""Hook re-arm engine for the Z6 II (eg1850_mas) equal-length patch.

To hook a firmware function we overwrite W bytes at the site with one `b.w` into a code hole.
Whatever we overwrote must still run, from a different address, with identical effect. Every
position-dependent instruction inside that window therefore has to be re-anchored:

  ldr  rX,[pc,#d]      -> movw rX,lo16(slot) ; movt rX,hi16(slot) ; ldr rX,[rX]
                          (the destination register doubles as the pointer, so no scratch
                           register is needed and no flags are touched)
  adr  rX,#abs         -> movw rX,lo16(abs) ; movt rX,hi16(abs)
  add  rX,pc,#d        -> same, with abs = align4(pc)+d
  b/bl/b<cc>           -> re-encoded to reach the same absolute target from the new location
  cbz/cbnz             -> same test, branching to the same absolute fall-through,
                          then an unconditional branch to the same absolute target

Anything the engine cannot relocate soundly (a store into a literal slot, an ldrd whose
pointer would need a spare register, a window that starts inside an IT block) is refused with
an exception, so the caller moves the window instead of emitting a wrong patch.

Verification is per group, not "bytes equal": the absolute addresses the original instruction
derived from PC must be exactly the absolute addresses materialised by the relocated group.
Keystone assembles, Capstone disassembles, and `_group_addr()` re-derives the value from the
re-disassembled stub -- three independent tools, one invariant.

Analysis / patch-generation only. Nothing here touches a camera.
"""
from __future__ import annotations

import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm_const import ARM_REG_PC
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

MAS_BASE = 0x10020000          # proven: 713 vs 15 string self-references (see build log)
SLA_BASE = 0x12520000

PC_REG = ARM_REG_PC          # 11 in this capstone build; never hardcode
_LO, _HI = 0xFFFF, 0xFFFF0000


class Refuse(Exception):
    """This window cannot be relocated soundly; move it or pick another hook site."""


def _cs():
    m = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    m.detail = True
    return m


def asm(text: str, at_vma: int, base: int = MAS_BASE) -> bytes:
    enc, n = _ks().asm(text, at_vma)
    if enc is None:
        raise Refuse(f'keystone rejected {text!r} @ {at_vma:#x}')
    return bytes(enc)


_ks_cache = {}


def _ks():
    return _ks_cache.get('k') or _ks_cache.setdefault('k', Ks(KS_ARCH_ARM, KS_MODE_THUMB))


def dis1(blob: bytes, off: int, base: int = MAS_BASE):
    return next(_cs().disasm(blob[off:off + 4], base + off, count=1), None)


def decode_run(blob: bytes, off: int, nbytes: int, base: int = MAS_BASE):
    """Decode exactly nbytes of whole instructions, starting at a known boundary."""
    ins, pos = [], 0
    for i in _cs().disasm(bytes(blob[off:off + nbytes]), base + off):
        if pos + i.size > nbytes:
            raise Refuse(f'f{off:#x}: window splits an instruction (ends mid {i.mnemonic})')
        ins.append(i)
        pos += i.size
    if pos != nbytes:
        raise Refuse(f'f{off:#x}: undecodable bytes in window (got {pos}/{nbytes})')
    return ins


def boundaries(blob: bytes, start: int, span: int, base: int = MAS_BASE):
    """[file_off] for every instruction start reached by linear decode from `start`."""
    pos, out = start, []
    for i in _cs().disasm(bytes(blob[start:start + span]), base + start):
        out.append(pos)
        pos += i.size
    return out


def _it_safe(blob: bytes, off: int, win: int, base: int = MAS_BASE) -> bool:
    """Thumb IT blocks make the following N instructions conditional and share a halfword
    schedule with them. A relocated window that begins inside one, or that contains an
    unreached part of one, would change behaviour -- so require no IT block within 8 bytes
    before the window and none inside it."""
    start = max(0, off - 8)
    pos = start
    for i in _cs().disasm(bytes(blob[start:start + 0x40]), base + start):
        f = start + (i.address - (base + start))
        if f >= off + win:
            return True
        if i.mnemonic == 'it':
            return False
        pos += i.size
    return False


def is_boundary(blob: bytes, off: int, base: int = MAS_BASE, lookback: int = 0x40,
                anchor: int = None):
    """Ground-truth instruction boundary.

    `anchor` is a caller-proven instruction start (a bl target, or an offset reached by
    linear decode from one); the answer is then simply whether the forward decode from
    anchor lands on `off`.  Without an anchor, walk back to the nearest `push` -- this
    image's usual opening instruction -- and use that as the anchor, which is a heuristic
    only in the choice of anchor, not in the check itself.
    """
    if anchor is not None:
        pos = anchor
        for x in _cs().disasm(bytes(blob[anchor:off + 8]), base + anchor):
            if pos == off:
                return True, f'aligned via caller-proven anchor f{anchor:#x}'
            pos += x.size
        return False, f'not reached from anchor f{anchor:#x}'
    for back in range(2, lookback, 2):
        s = off - back
        if s < 0:
            break
        i = dis1(blob, s, base)
        if i is None or i.mnemonic.split('.')[0] != 'push':
            continue
        pos = s
        for x in _cs().disasm(bytes(blob[s:off + 8]), base + s):
            if pos == off:
                return True, f'aligned via prologue f{s:#x} (+{back:#x})'
            pos += x.size
        return False, f'mid-instruction (prologue f{s:#x} +{back:#x})'
    try:
        decode_run(blob, off, 8, base)
        return True, 'no prologue in lookback; accepted on clean 8-byte decode'
    except Refuse as e:
        return False, str(e)


def pc_addr(i) -> list[tuple[str, int]]:
    """Absolute addresses this instruction derives from the PC."""
    out = []
    al = (i.address + 4) & ~3
    for o in i.operands:
        if o.type == 3 and o.mem.base == PC_REG and o.mem.index == 0:
            out.append(('literal', al + o.mem.disp))
    if i.mnemonic == 'adr':
        for o in i.operands[1:]:
            if o.type == 2:
                # capstone leaves ADR's own immediate unresolved here: ADR can only encode
                # 0..4095, so anything larger is already a resolved absolute address.
                out.append(('adr', al + o.imm if o.imm <= 0xFFF else o.imm))
    elif i.mnemonic in ('add', 'sub') and any(o.type == 1 and o.reg == PC_REG
                                              for o in i.operands[1:]):
        imm = [o.imm for o in i.operands if o.type == 2]
        if imm:
            out.append(('adr', al + imm[0]))
    if _is_branch(i.mnemonic):
        for o in i.operands:
            if o.type == 2:
                out.append(('branch', o.imm))
    return out


_BRANCHY = ('b', 'b.w', 'bl', 'bl.w', 'cbz', 'cbnz')

# A conditional branch means "taken -> target, not taken -> next".  Splitting it into
# <cc> + b would put the unconditional jump on the *not-taken* path, so the test has to be
# inverted and made to skip the jump.
INVERSE = {'eq': 'ne', 'ne': 'eq', 'hs': 'lo', 'cs': 'lo', 'lo': 'hs', 'cc': 'hs',
           'mi': 'pl', 'pl': 'mi', 'vs': 'vc', 'vc': 'vs', 'hi': 'ls', 'ls': 'hi',
           'ge': 'lt', 'lt': 'ge', 'gt': 'le', 'le': 'gt', 'al': None}


def _is_branch(mn: str) -> bool:
    if mn in _BRANCHY:
        return True
    if mn.startswith('b') and mn not in ('bic', 'bx', 'blx', 'bl', 'bfc', 'bfi', 'bkpt'):
        return len(mn) >= 3 and mn[1] != 'i'
    return False


def _cond_of(mn: str):
    return mn[1:] if _is_branch(mn) and mn not in _BRANCHY else None


def _plan(i, at: int, size_of):
    """Return (list of assembly lines, absolute addresses that must be materialised).

    `size_of(line)` encodes at a given address, which is needed because a near conditional
    branch is 2 bytes and a far one is 4 -- and its own target depends on its own size.
    """
    txt = i.mnemonic + ' ' + i.op_str
    base_mn = i.mnemonic.replace('.w', '')
    if base_mn in ('bl', 'blx'):
        raise Refuse(f'{i.address:#x}: window contains a call -- run from the stub it leaves lr '
                     f'inside the stub rather than back at the site, which the callee or a '
                     f'later bx lr can observe')
    addr = pc_addr(i)
    kinds = {k for k, _ in addr}
    if not addr:
        return [txt], set()                       # position-independent: copy verbatim
    if 'literal' in kinds:
        if i.mnemonic.startswith('st'):
            raise Refuse(f'{i.address:#x}: store into a literal slot needs a scratch reg')
        if i.mnemonic in ('ldrd', 'ldrex', 'ldaxr', 'strex'):
            raise Refuse(f'{i.address:#x}: {i.mnemonic} from literal not supported')
        dst = i.reg_name(i.operands[0].reg)
        if dst == 'pc':
            raise Refuse(f'{i.address:#x}: literal load into pc is a branch, not a load')
        slot = [v for k, v in addr if k == 'literal'][-1]
        ld = i.mnemonic + ' ' + dst + ', [' + dst + ']'
        return [_movw(dst, slot), _movt(dst, slot), ld], {slot}
    if 'adr' in kinds:
        dst = i.reg_name(i.operands[0].reg)
        val = [v for k, v in addr if k == 'adr'][-1]
        return [_movw(dst, val), _movt(dst, val)], {val}
    if 'branch' in kinds:
        tgt = [v for k, v in addr if k == 'branch'][-1]
        if i.mnemonic in ('cbz', 'cbnz') or _cond_of(i.mnemonic):
            if i.mnemonic in ('cbz', 'cbnz'):
                head = ('cbnz' if i.mnemonic == 'cbz' else 'cbz') + ' ' + \
                    i.reg_name(i.operands[0].reg)
            else:
                inv = INVERSE.get(_cond_of(i.mnemonic))
                if inv is None:
                    raise Refuse(f'{i.address:#x}: no inverse for condition '
                                 f'{_cond_of(i.mnemonic)!r}')
                head = 'b' + inv
            jump = f'b #{tgt:#x}'
            jsz = size_of(jump, at + 2)
            sz = 2
            for _ in range(4):                     # width and target depend on each other
                line = f'{head} #{at + sz + jsz:#x}'
                if size_of(line, at) == sz:
                    break
                sz = size_of(line, at)
            line = f'{head} #{at + sz + jsz:#x}'
            return [line, jump], {tgt, at + sz + jsz}
        return [f'b #{tgt:#x}'], {tgt}
    raise Refuse(f'{i.address:#x}: unsupported position-dependent form "{txt}"')


def _movw(rd, v):
    return f'movw {rd}, #{v & 0xFFFF:#x}'


def _movt(rd, v):
    return f'movt {rd}, #{v >> 16:#x}'


def group_addr(ins_list) -> set[int]:
    """Absolute addresses actually materialised by a disassembled relocated group:
    a movw/movt pair folds to its 32-bit value; a branch contributes its target."""
    out, pend = set(), {}
    for i in ins_list:
        if i.mnemonic in ('movw', 'movt'):
            rd = i.reg_name(i.operands[0].reg)
            imm = i.operands[1].imm
            lo, hi = pend.get(rd, (0, 0))
            if i.mnemonic == 'movw':
                lo = imm
            else:
                hi = imm << 16
            pend[rd] = (lo, hi)
        elif _is_branch(i.mnemonic):
            for o in i.operands:
                if o.type == 2:
                    out.add(o.imm)
    for lo, hi in pend.values():
        out.add((lo | hi) & 0xFFFFFFFF)
    return out


def rearm(blob: bytes, site_off: int, win: int, stub_off: int, resume_off: int,
          base: int = MAS_BASE, anchor: int = None):
    """Build the relocated window: a PC-re-anchored copy of blob[site_off:site_off+win]
    followed by `b.w #base+resume_off`.  All arguments are *file offsets*.
    Returns (bytes, provenance log).  Raises Refuse if it cannot be done soundly."""
    ok, why = is_boundary(blob, site_off, base, anchor=anchor)
    if not ok:
        raise Refuse(f'hook site f{site_off:#x} is not a boundary: {why}')
    if not _it_safe(blob, site_off, win, base):
        raise Refuse(f'hook site f{site_off:#x} sits in/after an IT block')
    ins = decode_run(blob, site_off, win, base)
    wlo, whi = base + site_off, base + site_off + win
    size_of = lambda text, at: len(asm(text, at, base))     # noqa: E731
    out = bytearray()
    log = []
    cur = base + stub_off
    resume_vma = base + resume_off
    for i in ins:
        if i.mnemonic.startswith('tb'):
            raise Refuse(f'{i.address:#x}: {i.mnemonic} takes its jump table from the PC, and '
                         f'no spare register is available to re-anchor it')
        lines, need = _plan(i, cur, size_of)
        inside = [v for v in need if wlo <= v < whi]
        if inside:
            raise Refuse(f'{i.address:#x}: window refers into itself at '
                         f'{[hex(v) for v in inside]} -- those bytes are about to be replaced '
                         f'by the hook, so the branch or literal would read the hook')
        body = b''
        for ln in lines:
            body += asm(ln, cur + len(body), base)
        got = list(_cs().disasm(bytes(body), cur))
        if sum(len(x.bytes) for x in got) != len(body):
            raise Refuse(f'{i.address:#x}: stub group does not re-disassemble cleanly')
        made = group_addr(got)
        if made != need:
            raise Refuse(f'{i.address:#x}: re-anchor drift {sorted(need)} != {sorted(made)} '
                         f'for "{"; ".join(lines)}')
        out += body
        log.append(f'{i.address:#010x}  {i.mnemonic + " " + i.op_str:32s} -> '
                   f'{" ; ".join(lines):56s} needs {sorted(hex(v) for v in need)}')
        cur += len(body)
    tail = asm(f'b #{resume_vma:#x}', cur, base)
    r = next(_cs().disasm(bytes(tail), cur), None)
    if r is None or r.operands[0].imm != resume_vma:
        raise Refuse(f'resume branch does not reach {resume_vma:#x}')
    out += tail
    log.append(f'{resume_vma:#010x}  <resume>                       -> '
               f'{"b #%x" % resume_vma:56s} (site+{win:#x}, not relocated)')
    return bytes(out), log


def hook_word(site_off: int, tramp_off: int, base: int = MAS_BASE) -> bytes:
    """The 4 bytes written over the hook site."""
    return asm(f'b #{base + tramp_off:#x}', base + site_off, base)
