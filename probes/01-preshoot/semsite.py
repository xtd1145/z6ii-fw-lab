"""Semantic profile of the eight installed hook sites.

For each site, inside its function, print everything that identifies it:
  * printable strings it loads (literal pool -> rodata, or adr)
  * 4-character tags it materialises with movw/movt (Nikon's 4CC vocabulary)
  * the functions it calls, each with the first string that function loads
  * the debug source-file names nearest to it in rodata
  * its callers, and the first string of each caller

Judgement is left to the reader on purpose: names such as BufRelAl came from a twin-vote
against Z6, and this is the evidence that either supports them or does not.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import lib                                     # noqa: E402

MAS = lib.load(lib.MAS)
BASE = lib.MAS_BASE
SITES = [
    ('capseq-reg-entry', 0x379F64),
    ('BufRelAl-entry', 0x206002),
    ('CapSt-pool-entry', 0x208A40),
    ('CaptureService-A', 0x2F6562),
    ('CaptureService-B', 0x2F8B5C),
    ('CaptureService-C', 0x390E34),
    ('FrontEnd-A', 0x20F986),
    ('CamCtl-InitiateCapture', 0x74910C),
]

md = lib.dis


def insns(off, limit=0x600):
    """Linear decode from a function entry until the epilogue or a limit."""
    out = []
    for v, f, m, ops in md(MAS, off, limit // 2):
        out.append((f, v, m, ops))
        if m == 'pop' and 'pc' in ops:
            break
        if m == 'bx' and 'lr' in ops:
            break
    return out


def str_of_literal(ins_list, f, ops):
    """the ASCII string this instruction points at, if any."""
    try:
        if '[pc' in ops:
            disp = int(ops.split('#')[-1].split(']')[0], 0)
            slot = ((f + BASE + 4) & ~3) + disp - BASE
            if 0 <= slot < len(MAS) - 4:
                tgt = lib.u32(MAS, slot) - BASE
                if 0 <= tgt < len(MAS):
                    s = lib.str_at(MAS, tgt, 4)
                    if s:
                        return s
        if ops.startswith('r') and '#' in ops:
            imm = int(ops.split('#')[-1], 0)
            if 0 < imm <= 0xFFF:
                tgt = ((f + BASE + 4) & ~3) + imm - BASE
                s = lib.str_at(MAS, tgt, 4)
                if s:
                    return s
    except (ValueError, IndexError):
        pass
    return ''


def callee_first_string(tgt, limit=0x300):
    for f, v, m, ops in insns(tgt, limit):
        s = str_of_literal(None, f, ops)
        if s and sum(c.isalpha() for c in s) >= 4:
            return s
        if m in ('movw', 'movt'):
            continue
    return ''


def tags_and_strings(off):
    ins = insns(off)
    strings, tags, calls = [], [], []
    lo16 = None
    for f, v, m, ops in ins:
        s = str_of_literal(ins, f, ops)
        if s:
            strings.append((f, s))
        if m == 'movw' and '#' in ops:
            try:
                lo16 = (int(ops.split('#')[-1], 0), f)
            except ValueError:
                lo16 = None
        elif m == 'movt' and '#' in ops and lo16:
            try:
                hi = int(ops.split('#')[-1], 0)
            except ValueError:
                lo16 = None
                continue
            val = (hi << 16) | (lo16[0] & 0xFFFF)
            b = val.to_bytes(4, 'little')
            if all(0x20 <= c < 0x7F for c in b):
                tags.append((lo16[1], b.decode(), hex(val)))
            lo16 = None
        elif m in ('bl', 'blx') and '#' in ops:
            try:
                calls.append((f, int(ops.split('#')[-1], 0) - BASE))
            except ValueError:
                pass
    return ins, strings, tags, calls


def srcfiles_near(off):
    """debug source-file names closest before and after the function."""
    arr = [(o, s) for o, s in lib.strings(MAS, 8) if s.endswith('.c') and '/' not in s]
    before = [(o, s) for o, s in arr if o <= off]
    after = [(o, s) for o, s in arr if o > off]
    out = []
    if before:
        out.append((off - before[-1][0], before[-1][1]))
    if after:
        out.append((off - after[0][0], after[0][1]))
    return out


def main():
    for name, off in SITES:
        ins, strings, tags, calls = tags_and_strings(off)
        print(f'\n=== {name}  f{off:07x}  ({len(ins)} instructions to epilogue)')
        print('  src-file neighbours:', '; '.join(f'{d:+d} {s}' for d, s in srcfiles_near(off)))
        uniq = []
        for f, s in strings:
            if s not in [x[1] for x in uniq] and sum(c.isalpha() for c in s) >= 3:
                uniq.append((f, s))
        print('  strings:', '; '.join(f'{s!r}' for _, s in uniq[:12]) or 'none')
        if tags:
            print('  4CC tags:', ', '.join(f'{t}({v})' for _, t, v in tags[:10]))
        seen = set()
        for f, tgt in calls[:16]:
            if tgt in seen:
                continue
            seen.add(tgt)
            print(f'    bl f{f:07x} -> f{tgt:07x}  first string {callee_first_string(tgt)!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
