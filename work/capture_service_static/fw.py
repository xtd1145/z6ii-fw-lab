"""Read-only firmware loader / disassembler helpers for Z6 II 1.70 (eg1850_*).

Address model (verified, NOT the old 0x10000000 base):
    VMA = file_offset + 0x10020000
"""
import os, struct, functools
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN
from capstone import arm_const as AC

ROOT = r'D:\z6ii_fw_lab'
BASE = 0x10020000

IMAGES = {
    'mas': os.path.join(ROOT, 'fw_verified', 'eg1850_mas_01700'),
    'sla': os.path.join(ROOT, 'fw_verified', 'eg1850_sla_01700'),
    'bi':  os.path.join(ROOT, 'fw_verified', 'ex1850_012000.bi'),
}

_cache = {}


def img(name):
    if name not in _cache:
        with open(IMAGES[name], 'rb') as f:
            _cache[name] = f.read()
    return _cache[name]


def size(name):
    return len(img(name))


def in_image(name, a):
    return BASE <= a < BASE + size(name)


def vma(name, off):
    return off + BASE


def rd32(name, a):
    return struct.unpack_from('<I', img(name), a - BASE)[0]


def rd16(name, a):
    return struct.unpack_from('<H', img(name), a - BASE)[0]


def raw(name, a, n):
    return img(name)[a - BASE: a - BASE + n]


def cstr(name, a, maxlen=256):
    if not in_image(name, a):
        return None
    d = img(name); o = a - BASE
    e = d.find(b'\0', o, o + maxlen)
    if e < 0:
        return None
    try:
        s = d[o:e].decode('ascii')
    except UnicodeDecodeError:
        return None
    return s


def printable(name, a, maxlen=64):
    s = cstr(name, a, maxlen)
    if s is None:
        return None
    if len(s) < 4:
        return None
    if sum(1 for c in s if 32 <= ord(c) < 127) != len(s):
        return None
    return s


@functools.lru_cache(maxsize=4)
def md_thumb():
    m = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    m.detail = True
    return m


def dis_thumb(name, a, n):
    return list(md_thumb().disasm(raw(name, a, n), a))


def lit_refs(name, a, n):
    """Yield (insn_addr, loaded_value, op_str) for PC-literal loads in range."""
    for ins in dis_thumb(name, a, n):
        if ins.id not in (AC.ARM_INS_LDR, AC.ARM_INS_LDRB, AC.ARM_INS_LDRH,
                          AC.ARM_INS_LDRSB, AC.ARM_INS_LDRSH, AC.ARM_INS_VLDR):
            continue
        if len(ins.operands) < 2:
            continue
        mem = ins.operands[-1]
        if mem.type != AC.ARM_OP_MEM or mem.mem.base != AC.ARM_REG_PC:
            continue
        # capstone reports PC-relative disp as offset from align(addr+4,4)
        litaddr = ((ins.address + 4) & ~3) + mem.mem.disp
        if not in_image(name, litaddr):
            continue
        yield ins.address, rd32(name, litaddr), ins.op_str


def direct_targets(name, a, n):
    """Yield (insn_addr, target) for B/BL with immediate target."""
    for ins in dis_thumb(name, a, n):
        if ins.id in (AC.ARM_INS_BL, AC.ARM_INS_B, AC.ARM_INS_BLX) and ins.operands:
            op = ins.operands[0]
            if op.type == AC.ARM_OP_IMM:
                yield ins.address, op.imm & 0xFFFFFFFF, ins.id
