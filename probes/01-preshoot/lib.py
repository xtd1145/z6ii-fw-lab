"""Shared read-only helpers for the Z6 II memory audit.

Every image is opened from the repo, never written.  File offset -> VMA is
    vma = file_off + BASE
with BASE settled by t1_base.py (see verify.py for the byte assertions).
"""
from pathlib import Path
import numpy as np
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, CS_MODE_V8

ROOT = Path(__file__).resolve().parents[2]
MAS = str(ROOT / 'extracted/eg1850_mas_01700')
SLA = str(ROOT / 'extracted/eg1850_sla_01700')
Z6_STOCK = str(ROOT / 'all_modules/Z_6_0380/eg1620_038000.bi')
Z6_PATCH = str(ROOT / 'work/wlz6/EG162080_en.BIN')

# settled by t1_base.py; asserted in verify.py
MAS_BASE = 0x10020000
SLA_BASE = 0x12520000
Z6_BASE = 0x10020000

_m = {}


def img(path):
    if path not in _m:
        _m[path] = Path(path).read_bytes()
    return _m[path]


load = img


def u32(b, off):
    return int(np.frombuffer(memoryview(b)[off:off + 4], dtype='<u4')[0])


def u16(b, off):
    return int(np.frombuffer(memoryview(b)[off:off + 2], dtype='<u2')[0])


def words(b):
    n = (len(b) - 4) & ~3
    return np.frombuffer(memoryview(b)[:n], dtype='<u4')


def f2v(f, base=MAS_BASE):
    return f + base


def v2f(v, base=MAS_BASE):
    return v - base


def dis(b, off, n=40, base=MAS_BASE, arm=False):
    """Linear Thumb (default) / ARM disassembly from file offset."""
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM if arm else (CS_MODE_THUMB | CS_MODE_V8))
    md.skipdata = True
    span = n * 4 if arm else n * 2
    data = bytes(b[off:off + span])
    out = []
    for ins in md.disasm(data, f2v(off, base)):
        out.append((ins.address, off + (ins.address - f2v(off, base)), ins.mnemonic, ins.op_str))
    return out


def show(b, off, n=40, base=MAS_BASE, arm=False):
    for v, o, m, ops in dis(b, off, n, base, arm):
        print(f'  {v:08x} f{o:07x} {m:<10} {ops}')


PUSH = (0xb4, 0xb5)  # push {...}, push {...,lr}


def pro_bits(lo, hi):
    """True for a plausible Thumb function entry given its first two bytes.

    Accepted: push {reglist[,lr]} (hi=0xB4/0xB5), sub/add sp,imm (hi=0xB0..0xB7),
    push.w {reglist} (lo,hi = 0x2D,0xE9), sub.w sp,sp,#imm (lo,hi = 0xA0/0xB0,0xF1).
    """
    if hi in (0xB4, 0xB5) or (0xB0 <= hi <= 0xB7):
        return True
    if lo == 0x2D and hi == 0xE9:
        return True
    if hi == 0xF1 and lo in (0xA0, 0xB0):
        return True
    return False


def is_prologue(b, off):
    """Plausible Thumb function entry: push with lr, or a frame-setup."""
    if off < 0 or off + 2 > len(b):
        return False
    return pro_bits(b[off], b[off + 1])


def str_at(b, off, minlen=4):
    """the NUL-terminated printable ASCII string starting at off ('' if none)."""
    s = bytearray()
    i = off
    while i < len(b) and 0x20 <= b[i] < 0x7F:
        s.append(b[i])
        i += 1
    return s.decode('latin1') if len(s) >= minlen else ''


def ascii_at(b, off, minlen=6):
    """non-empty string == True (kept for readability at call sites)."""
    return str_at(b, off, minlen)


def find_all(b, pat, start=0, limit=100000):
    """all file offsets of a byte pattern (up to limit hits)."""
    out = []
    i = b.find(pat, start)
    while i >= 0 and len(out) < limit:
        out.append(i)
        i = b.find(pat, i + 1)
    return out


def strings(b, minlen=6):
    """[(off, s)] for printable ASCII runs of >= minlen bytes (numpy run detection)."""
    arr = np.frombuffer(b, dtype=np.uint8)
    ok = (arr >= 0x20) & (arr < 0x7F)
    padded = np.concatenate(([False], ok, [False]))
    d = np.diff(padded.astype(np.int8))
    starts = np.nonzero(d == 1)[0]
    ends = np.nonzero(d == -1)[0]
    lens = ends - starts
    keep = lens >= minlen
    starts, ends = starts[keep], ends[keep]
    return [(int(s), b[s:e].decode('latin1')) for s, e in zip(starts, ends)]
