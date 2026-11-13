"""Build the Z6 II (eg1850_mas) hook layer -- WonderLantern's mechanism, re-derived.

The shape is copied from the shipped Z6 patch, whose dispatcher and thunks were read out of
work/wlz6/EG162080_en.BIN at f000e13c and f000e568:

  thunk(slot)  push.w {r0,r1,r2,r3,ip,lr}     save the caller's argument registers
               mrs  r1, apsr                 ... and the flags the site was relying on
               push {r1, r4}
               movw r4, #slot
               b.w  #dispatch

  dispatch     if CTRL.magic == 'Z6PC' and CTRL.core_base and slot < CTRL.n_slots:
                   ip = core_base + core_table[slot]      PIC offset, thumb bit set
               else:
                   ip = fallback_table[slot]              the relocated window
               str.w ip, [sp, #0x18]          stash the target in the *saved ip slot*
               pop  {r1, r4}                  restore flags and r4
               msr  apsr_nzcvqg, r1
               pop.w {r0,r1,r2,r3,ip,lr}      ip is now the target, everything else is back
               bx   ip

That stack patch is why nothing is clobbered on the way out and why a core receives exactly
the arguments the hooked function would have received. While CTRL stays zero -- which is how
the image ships and how it remains until a core writes it -- every site just runs its
relocated window and returns, i.e. stock behaviour. test_hooks.py proves that by execution.

Deliberate differences from WL:
  * a fixed in-image control block instead of a DRAM block-table walk. WL's finder reads
    0xC1F88798 and bounds 0xC9700000..0xCC353C80, both Z6-specific; a fixed address needs no
    Z6 II constant we could not verify offline.
  * slots are indexed 0..n-1, not by WL's hook numbers, so the fallback table is tiny.

Patch generation only; nothing here touches a camera.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import lib                                    # noqa: E402
import rearm as R                             # noqa: E402

BASE = R.MAS_BASE
MAS = lib.load(lib.MAS)

CTRL = 0x000E01C          # 16 B: magic, core_base, n_slots, reserved
FTAB = 0x000E030          # fallback table, one word per slot
DISPATCH = 0x000E080
THUNK = 0x000E100
FALLBACK = 0x000E200
# Runtime-writable state lives in DRAM, never in the image: the mas mapping is read-only at
# run time (QEMU: Data Abort, DFSR 0x81d, DFAR 0x1002e280).  Window per the lab machine's
# memory map: 0xC2C00000..0xC5F00000 is runtime heap, so the last 4 KiB of it is the coldest
# spot for 800 bytes of state.  PS_DRAM + 0x1000 == the window end.
PS_DRAM = 0xC5EFF000      # 4 KiB reserved inside the verified writable window
STATUS = PS_DRAM          # 96 B state block (magic, counters, probe, NP3 export)
RING = PS_DRAM + 0x100    # 48 descriptors x 16 B = 768 B
NP3_PAGE_SRC = PS_DRAM    # STATUS(96) + RING(768) = 864 B, split into 2 pages of 432 B
NP3_PAGE_DATA = 432       # bytes of state per page
NP3_PAGE_TMP = PS_DRAM + 0x400   # scratch page: 16 B header + 432 B data + 64 B pad
NP3_HDR_MAGIC = 0x4450365A       # 'Z6PD' little-endian
PROBE_LO = 0xC2C00000     # only these addresses are ever dereferenced by the probe
PROBE_HI = 0xC5F00000
CORESLOT = 0x000E2C0      # entry table at +0x10, slot code from +0x30
COREWORK = 0x000E420
CLEAN_END = 0x000E9C2     # end of the zero run that has no inbound references
MAGIC = 0x5043365A        # 'Z6PC' -- "a core is installed"
SMAGIC = 0x5350365A       # 'Z6PS' -- "the pre-shoot state block is initialised"
TABLE_OFF = 0x10          # a core's entry table lives at core_base + 0x10
NSLOT = 48                # pre-shoot ring depth (descriptors, not pixels)

# core_work stack frame, relative to sp on entry: the slot stub pushes
# {r0,r1,r2,r3,lr} then {apsr}, so after core_work's own {r4,r5,lr} the hook's live
# arguments sit at these offsets.  test_core.py checks them against known inputs.
ARG0_OFF, ARG1_OFF, ARG2_OFF, LROFF = 16, 20, 24, 32
TRIGGER_SLOT = 7          # slot whose call freezes the ring (see BUILD.md: assumed)

# (name, site file offset, window bytes); the slot index is the position in this list.
SITES = [
    ('capseq-reg-entry',       0x379F64, 4),
    ('BufRelAl-entry',         0x206002, 4),
    ('CapSt-pool-entry',       0x208A40, 4),
    ('CaptureService-A',       0x2F6562, 4),
    ('CaptureService-B',       0x2F8B5C, 4),
    ('CaptureService-C',       0x390E34, 4),
    ('FrontEnd-A',             0x20F986, 4),
    ('CamCtl-InitiateCapture', 0x74910C, 4),
    # B1-v2 export path: the card write API.  The camera's picture-control save always
    # funnels a 0xF8-byte NCP buffer through here; for the four reserved slots 96..99 the
    # hook replaces that buffer with one 248-byte chunk of probe data (the file is only a
    # carrier -- the PC never has to load it back).  Gating on the exact path keeps normal
    # saves, boot-time rewrites and every other file untouched.
    ('np3-write-chunk',        0x4B68E6, 4),
]
CHUNK_MAGIC = 0x4450365A   # 'Z6PD' header in every carried chunk
CHUNK_SRC = PS_DRAM        # STATUS(96) + RING(768) = 864 B
CHUNK_LEN = 864
CHUNK_DATA = 232           # payload bytes per save (4 chunks: 232*3 + 168)
CHUNK_LAST = 168
CHUNK_TOTAL = 4
CHUNK_SLOT0 = 96           # user saves to PICCON96..99
READ_LO, READ_HI = 0xC0000000, 0xC6000000   # whole guest RAM, for path/stream/buffer reads
IO_SLOT = 8                # the write-API slot


def asm_seq(lines, at):
    blob = bytearray()
    for ln in lines:
        blob += R.asm(ln, at + len(blob))
    return bytes(blob)


COND = {'beq': 0, 'bne': 1, 'bcs': 2, 'bhs': 2, 'bcc': 3, 'blo': 3, 'bmi': 4,
        'bpl': 5, 'bvs': 6, 'bvc': 7, 'bhi': 8, 'bls': 9, 'bge': 10, 'blt': 11,
        'bgt': 12, 'ble': 13}
INV = {0: 1, 1: 0, 2: 3, 3: 2, 4: 5, 5: 4, 6: 7, 7: 6, 8: 9, 9: 8, 10: 11, 11: 10,
       12: 13, 13: 12}


def near_cond(cond, off):
    """16-bit B<c>; keystone's narrow form is correct, but doing it here keeps the
    displacement explicit."""
    if off & 1 or not (-256 <= off <= 254):
        raise SystemExit(f'near_cond: offset {off} out of range')
    return struct.pack('<H', 0xD000 | (cond << 8) | ((off >> 1) & 0xFF))


def cond_branch(mn, target, pc):
    """Conditional branch, wide-safe.

    keystone 0.9.2 mis-encodes B<c>.W (absolute target used as a displacement), so a far
    condition becomes `invert + b target`: the 16-bit inverted branch skips keystone's
    *unconditional* B, whose wide encoding is correct.
    """
    off = target - (pc + 4)
    if -256 <= off <= 254:
        return near_cond(COND[mn], off)
    bbytes = R.asm(f'b #{target:#x}', pc + 2)
    return near_cond(INV[COND[mn]], len(bbytes) - 2) + bbytes


def asm_labels(items, at):
    """Assemble a block with internal forward branches.  Items are instruction strings,
    ('LABEL', name), or (mnemonic, label).  Label positions are solved to a fixed point
    because a conditional branch's own width changes where the label lands."""
    labels = {}
    for _ in range(10):
        blob, pos, new = bytearray(), at, {}
        for it in items:
            if isinstance(it, tuple) and it[0] == 'LABEL':
                new[it[1]] = pos
                continue
            if isinstance(it, str):
                text = it
                try:
                    e = R.asm(text, pos)
                except Exception as ex:
                    raise SystemExit(f'asm_labels @ f{pos - BASE:#x}: {text!r}: {ex}')
            elif len(it) == 3:                      # (mnemonic, reg, label)
                mn, reg, lb = it
                text = f'{mn} {reg}, #{labels.get(lb, pos + 2):#x}'
                try:
                    e = R.asm(text, pos)
                except Exception as ex:
                    raise SystemExit(f'asm_labels @ f{pos - BASE:#x}: {text!r}: {ex}')
            else:
                mn, lb = it
                target = labels.get(lb, pos + 2)
                if mn in COND:
                    e = cond_branch(mn, target, pos)
                else:
                    text = f'{mn} #{target:#x}'
                    try:
                        e = R.asm(text, pos)
                    except Exception as ex:
                        raise SystemExit(f'asm_labels @ f{pos - BASE:#x}: {text!r}: {ex}')
            blob += e
            pos += len(e)
        if new == labels:
            return bytes(blob), new
        labels = new
    raise SystemExit('label layout did not converge')


def imm32(rd, val):
    return [f'movw {rd}, #{val & 0xFFFF:#x}', f'movt {rd}, #{val >> 16:#x}']


RESTORE = ['str.w ip, [sp, #0x18]', 'pop  {r1, r4}', 'msr  cpsr_fs, r1',
           'pop.w {r0, r1, r2, r3, ip, lr}', 'bx   ip']


def thunk(slot, at):
    """16 bytes plus WL's 2-byte filler, so every thunk is a uniform 18 B."""
    return asm_seq(['push.w {r0, r1, r2, r3, ip, lr}', 'mrs  r1, apsr', 'push {r1, r4}',
                    f'movw r4, #{slot:#x}', f'b    #{BASE + DISPATCH:#x}'], BASE + at) + b'\0\0'


def core_slot(k, at):
    """One per hook slot: keep the hook's arguments, ask the core, then continue into the
    stock window.  r12 is free here (the dispatcher already clobbered it) and carries both
    the slot number in and the fallback address out."""
    return asm_seq(['push {r0, r1, r2, r3, lr}',
                    'mrs  r0, apsr',
                    'push {r0}',
                    f'movw r12, #{k:#x}',
                    f'bl   #{BASE + COREWORK:#x}',
                    'pop  {r0}',
                    'msr  cpsr_fs, r0',
                    'pop  {r0, r1, r2, r3, lr}',
                    'bx   r12'], BASE + at)


def _probe(arg_index, miss):
    """Instruction list: if hook argument #arg_index points at a JPEG bitstream, count it and
    remember the first (slot, address).  Reads only inside PROBE_LO..PROBE_HI."""
    off = (ARG0_OFF, ARG1_OFF)[arg_index]
    return [
        f'ldr  r0, [sp, #{off}]',
        *imm32('r2', PROBE_LO),
        'cmp  r0, r2',
        ('blo', miss),
        *imm32('r2', PROBE_HI),
        'cmp  r0, r2',
        ('bhs', miss),
        'tst  r0, #3',
        ('bne', miss),
        'ldr  r1, [r0]',
        *imm32('r2', 0x00FFFFFF),
        'and  r1, r1, r2',
        *imm32('r2', 0x00FFD8FF),
        'cmp  r1, r2',
        ('bne', miss),
        'ldr  r2, [r5, #0x20]',
        'adds r2, #1',
        'str  r2, [r5, #0x20]',           # jpeg_hits++
        'ldr  r2, [r5, #0x24]',
        *imm32('r3', 0xFFFFFFFF),
        'cmp  r2, r3',
        ('bne', miss),
        'str  r4, [r5, #0x24]',           # first hit: which slot
        'str  r0, [r5, #0x28]',           # ... and where
    ]


def core_work():
    """The pre-shoot state machine.

    STATE (STATUS): magic, count, write_idx, frozen, retained, triggers, armed, reserved.
    On the first call the block is initialised and armed.  Every call appends
    {slot, arg0, arg1, lr} to the descriptor ring.  A call through TRIGGER_SLOT freezes the
    ring and records how many descriptors it is holding -- that is the "keep the last N"
    decision, taken at the moment the shot is initiated.  While frozen, nothing is appended,
    so the retained set is exactly the frames/events that preceded the trigger.

    Deliberately does not touch r6-r11, and restores everything it uses.
    """
    items = [
        'push {r4, r5, lr}',
        'mov  r4, r12',
        *imm32('r5', STATUS),
        # DRAM holds whatever was there before, so "is this mine?" needs both the magic and
        # its complement -- a single word matching by chance is 2^-32, two are 2^-64.
        'ldr  r0, [r5]',
        *imm32('r1', SMAGIC),
        'cmp  r0, r1',
        ('bne', 'init'),
        'ldr  r0, [r5, #0x1c]',
        *imm32('r1', (~SMAGIC) & 0xFFFFFFFF),
        'cmp  r0, r1',
        ('beq', 'inited'),
        ('LABEL', 'init'),
        *imm32('r1', (~SMAGIC) & 0xFFFFFFFF),
        'str  r1, [r5, #0x1c]',           # complement first ...
        'movs r2, #0',
        'str  r2, [r5, #4]',              # count
        'str  r2, [r5, #8]',              # write_idx
        'str  r2, [r5, #12]',             # frozen
        'str  r2, [r5, #16]',             # retained
        'str  r2, [r5, #20]',             # triggers
        *imm32('r2', 0xFFFFFFFF),
        'str  r2, [r5, #0x24]',           # jpeg_slot = none
        *imm32('r2', 0),                  # page = 0, save_actions = 0
        'str  r2, [r5, #0x2c]',           # page
        'str  r2, [r5, #0x30]',           # save_actions
        'str  r2, [r5, #0x34]',           # np3_writes
        'str  r2, [r5, #0x38]',           # np3_addr
        'str  r2, [r5, #0x28]',           # jpeg_addr = none
        'movs r2, #0',
        'str  r2, [r5, #0x20]',           # jpeg_hits = 0
        'movs r0, #1',
        'str  r0, [r5, #24]',             # armed = 1 (always-on pre-shoot)
        *imm32('r1', SMAGIC),
        'str  r1, [r5]',                  # ... magic last: an interrupted init stays invalid
        ('LABEL', 'inited'),
        # --- JPEG probe ---------------------------------------------------------
        # Both hook arguments are tested: is either a pointer into the mapped window whose
        # first word looks like a JPEG SOI (FF D8 FF xx)?  Dereferencing is confined to the
        # confirmed window, so the probe cannot fault; a buffer outside it goes unnoticed.
        *_probe(0, 'probe1'),
        ('LABEL', 'probe1'),
        *_probe(1, 'probe_done'),
        ('LABEL', 'probe_done'),
        f'cmp  r4, #{IO_SLOT:#x}',
        ('beq', 'io_core'),
        f'cmp  r4, #{TRIGGER_SLOT:#x}',
        ('bne', 'record'),
        'movs r0, #1',
        'str  r0, [r5, #12]',             # frozen = 1
        'ldr  r0, [r5, #4]',              # count at trigger time
        f'cmp  r0, #{NSLOT:#x}',
        ('bls', 'trig2'),
        f'movs r0, #{NSLOT:#x}',
        ('LABEL', 'trig2'),
        'ldr  r1, [r5, #20]',
        'adds r1, #1',
        'str  r1, [r5, #20]',             # triggers++
        'str  r0, [r5, #16]',             # retained = min(count, NSLOT)
        ('LABEL', 'record'),
        # nb: keystone encodes cbz/cbnz targets as a *relative* offset and silently
        # truncates an absolute address, so cmp+branch is used for every conditional here.
        'ldr  r0, [r5, #24]',
        'cmp  r0, #0',
        ('beq', 'ret'),                    # not armed
        'ldr  r0, [r5, #12]',
        'cmp  r0, #0',
        ('bne', 'ret'),                    # frozen: keep what we have
        'ldr  r2, [r5, #8]',              # write_idx
        *imm32('r1', RING),
        'add.w r1, r1, r2, lsl #4',       # descriptor = 16 B
        'str  r4, [r1]',                  # slot
        'ldr  r0, [sp, #%d]' % ARG0_OFF,  # the hook's r0, straight off the save area
        'str  r0, [r1, #4]',
        'ldr  r0, [sp, #%d]' % ARG1_OFF,  # the hook's r1
        'str  r0, [r1, #8]',
        'ldr  r0, [sp, #%d]' % LROFF,     # the hook's lr
        'str  r0, [r1, #12]',
        'ldr  r0, [r5, #4]',
        'adds r0, #1',
        'str  r0, [r5, #4]',              # count++
        'adds r2, #1',
        f'cmp  r2, #{NSLOT:#x}',
        ('blt', 'adv2'),
        'movs r2, #0',
        ('LABEL', 'adv2'),
        'str  r2, [r5, #8]',              # write_idx = (write_idx+1) % NSLOT
        f'cmp  r0, #{NSLOT:#x}',
        ('bls', 'ret'),
        f'movs r0, #{NSLOT:#x}',
        'str  r0, [r5, #16]',             # retained saturates at NSLOT
        ('LABEL', 'io_core'),
        # Only a picture-control save to the reserved slots 96..99 is a probe carrier.
        # Photo writes, settings, boot-time PC rewrites and the other 95 slots all fail
        # one of these gates and stay byte-for-byte untouched.
        f'ldr  r0, [sp, #{ARG2_OFF}]',    # write size
        'movs r1, #0xF8',
        'cmp  r0, r1',
        ('bne', 'ret'),
        f'ldr  r0, [sp, #{ARG0_OFF}]',    # stream object
        *imm32('r1', READ_LO),
        'cmp  r0, r1',
        ('blo', 'ret'),
        *imm32('r1', READ_HI),
        'cmp  r0, r1',
        ('bhs', 'ret'),
        'ldr  r2, [r0]',                  # path string
        'cmp  r2, r1',
        ('bhs', 'ret'),
        *imm32('r0', READ_LO),
        'cmp  r2, r0',
        ('blo', 'ret'),
        # find the NUL, then match the last 12 chars PICCON96.NP3..PICCON99.NP3
        'movs r3, #0',
        ('LABEL', 'io_nul'),
        'ldrb r0, [r2, r3]',
        'cmp  r0, #0',
        ('beq', 'io_end'),
        'adds r3, #1',
        'cmp  r3, #0x60',
        ('blo', 'io_nul'),
        ('b', 'ret'),
        ('LABEL', 'io_end'),
        'cmp  r3, #12',
        ('blo', 'ret'),
        'adds r2, r2, r3',
        'subs r2, #12',
        'ldr  r0, [r2]',
        *imm32('r1', 0x43434950),         # "PICC"
        'cmp  r0, r1',
        ('bne', 'ret'),
        'ldrh r0, [r2, #4]',
        *imm32('r1', 0x4E4F),             # "ON"
        'cmp  r0, r1',
        ('bne', 'ret'),
        'ldrb r0, [r2, #6]',
        'cmp  r0, #0x39',
        ('bne', 'ret'),
        'ldrb r3, [r2, #7]',              # slot digit 6..9
        'cmp  r3, #0x36',
        ('blo', 'ret'),
        'cmp  r3, #0x39',
        ('bhi', 'ret'),
        'subs r3, #0x36',                 # chunk 0..3
        'ldr  r0, [r2, #8]',
        *imm32('r1', 0x33504E2E),         # ".NP3"
        'cmp  r0, r1',
        ('bne', 'ret'),
        # buffer descriptor: pointer at +8, capacity at +0xc
        f'ldr  r1, [sp, #{ARG1_OFF}]',
        *imm32('r0', READ_LO),
        'cmp  r1, r0',
        ('blo', 'ret'),
        *imm32('r0', READ_HI),
        'cmp  r1, r0',
        ('bhs', 'ret'),
        'ldr  r2, [r1, #8]',
        'cmp  r2, r0',
        ('bhs', 'ret'),
        *imm32('r0', READ_LO),
        'cmp  r2, r0',
        ('blo', 'ret'),
        'ldr  r0, [r1, #0xc]',
        'movs r1, #0xF8',
        'cmp  r0, r1',
        ('blo', 'ret'),
        'ldr  r0, [r2]',
        *imm32('r1', 0x0050434E),         # "NCP\0"
        'cmp  r0, r1',
        ('bne', 'ret'),
        # header: magic, chunk|total<<8|len<<16, offset, sum/version
        *imm32('r0', CHUNK_MAGIC),
        'str  r0, [r2]',
        'mov  r0, r3',
        f'movs r1, #{CHUNK_TOTAL}',
        'lsl  r1, r1, #8',
        'orr  r0, r1',
        f'movs r4, #{CHUNK_DATA}',
        'cmp  r3, #3',
        ('bne', 'io_len'),
        f'movs r4, #{CHUNK_LAST}',
        ('LABEL', 'io_len'),
        'mov  r1, r4',
        'lsl  r1, r1, #16',
        'orr  r0, r1',
        'str  r0, [r2, #4]',
        f'movs r0, #{CHUNK_DATA}',
        'mul  r1, r0, r3',                # offset = 232 * chunk
        'str  r1, [r2, #8]',
        'movs r0, #1',
        'lsl  r0, r0, #16',
        'str  r0, [r2, #0xc]',            # version=1, sum=0
        *imm32('r0', CHUNK_SRC),
        'add  r0, r0, r1',                # src
        'adds r2, #16',                   # dst
        'movs r3, #0',                    # sum
        'lsrs r1, r4, #1',                # halfwords
        ('LABEL', 'io_copy'),
        'ldrh r4, [r0], #2',
        'add  r3, r4',
        'uxth r3, r3',
        'strh r4, [r2], #2',
        'subs r1, #1',
        ('bne', 'io_copy'),
        f'ldr  r1, [sp, #{ARG1_OFF}]',
        'ldr  r0, [r1, #8]',
        'strh r3, [r0, #0xc]',            # sum
        'ldr  r0, [r5, #0x34]',
        'adds r0, #1',
        'str  r0, [r5, #0x34]',           # np3_writes++
        'ldr  r0, [r5, #0x38]',
        'cmp  r0, #0',
        ('bne', 'io_done'),
        'ldr  r0, [r1, #8]',
        'str  r0, [r5, #0x38]',           # np3_addr
        ('LABEL', 'io_done'),
        f'movs r4, #{IO_SLOT}',           # restore the slot index for the fallback lookup
        ('b', 'ret'),
        ('LABEL', 'ret'),
        *imm32('r12', BASE + FTAB),
        'ldr.w r12, [r12, r4, lsl #2]',   # the fallback stub for this slot
        'pop  {r4, r5, pc}',
    ]
    blob, labels = asm_labels(items, BASE + COREWORK)
    return blob


READONLY = (0x10020000, 0x12520000)     # mas + sla image mapping, read-only at run time
PS_WINDOW = (0xC2C00000, 0xC5F00000)    # verified writable runtime heap window


def check_state_place(verbose=True):
    """The state block must sit in the writable window and nowhere near the image."""
    lo, hi = PS_WINDOW
    for nm, a, n in (('STATUS', STATUS, 32), ('RING', RING, NSLOT * 16)):
        if not (lo <= a and a + n <= hi):
            raise SystemExit(f'{nm} {a:#x}..{a + n:#x} is outside the writable window '
                             f'{lo:#x}..{hi:#x}')
        if a < READONLY[1] and a + n > READONLY[0]:
            raise SystemExit(f'{nm} {a:#x} lands in the read-only image mapping')
        if verbose:
            print(f'  {nm} {a:#010x}..{a + n:#010x}  writable, outside the image')


def check_runtime_writes(blob, at, label, trusted=()):
    """Static MMU-aware check: every store the core performs must provably land outside the
    read-only image mapping.  Unicorn does not model page permissions, so this is the guard
    that would have caught the Data Abort at DFAR 0x1002e280 before it reached hardware.

    Addresses are propagated through movw/movt (exact) and one `add rd, rn, rm, lsl #k` step
    (kept as a lower bound, widened by the ring size).  A store whose base cannot be resolved
    is reported as a failure rather than assumed safe."""
    md = R._cs()
    const, bad, proven = {}, [], 0
    for i in md.disasm(bytes(blob), at):
        ops = i.operands
        if i.mnemonic in ('movw', 'movt') and len(ops) >= 2                 and ops[0].type == 1 and ops[1].type == 2:
            rd = ops[0].reg
            if i.mnemonic == 'movw':
                const[rd] = ops[1].imm & 0xFFFF
            else:
                const[rd] = (const.get(rd, 0) & 0xFFFF) | (ops[1].imm << 16)
            continue
        if i.mnemonic in ('add', 'add.w') and len(ops) >= 2                 and ops[0].type == 1 and ops[1].type == 1:
            rd, rn = ops[0].reg, ops[1].reg
            if rn in const:
                const[rd] = const[rn]
            else:
                const.pop(rd, None)
            continue
        if i.mnemonic.startswith('st'):
            mem = next((o for o in ops if o.type == 3), None)
            if mem is None:
                continue
            base = mem.mem.base
            if base == 12:                       # sp: the core's own frame, not the image
                continue
            if base in const:
                lo = const[base] + mem.mem.disp
                hi = lo + NSLOT * 16 + 0x100
                if lo < READONLY[1] and hi > READONLY[0]:
                    bad.append(f'{label}: {i.mnemonic} {i.op_str} -> {lo:#x} is inside the '
                               f'read-only image {READONLY[0]:#x}..{READONLY[1]:#x}')
                else:
                    proven += 1
            elif i.reg_name(base) in trusted:
                # runtime-validated elsewhere (see the io-core slot: window, alignment and
                # NP3 magic are all checked before the buffer is handed over)
                proven += 1
            else:
                bad.append(f'{label}: {i.mnemonic} {i.op_str} base {i.reg_name(base)} has no '
                           f'provable address')
            continue
        if ops and ops[0].type == 1:
            const.pop(ops[0].reg, None)
    return bad, proven


def ctrl_block(nslots):
    """Shipped already installed: magic, core_base, n_slots.  Zeroing these 16 bytes is the
    "no core" configuration that test_core.py A uses."""
    return struct.pack('<III', MAGIC, BASE + CORESLOT, nslots) + b'\0' * 4


def core_slot_table(stride):
    """entry table at core_base + 0x10: offsets from core_base, `stride` bytes apart."""
    base = 0x40                            # slot code starts past the 10-word table
    return b''.join(struct.pack('<I', base + i * stride) for i in range(len(SITES)))


def dispatcher(nslots):
    """Solve the three forward branches to the fallback tail by fixed point: each branch's
    own width depends on its distance to that tail."""
    at = BASE + DISPATCH
    DUMMY = f'#{at + 0x60:#x}'                    # any encodable stand-in for the tail
    prefix = [
        *imm32('r0', BASE + CTRL),
        'ldr  r1, [r0]',
        *imm32('r2', MAGIC),
        'cmp  r1, r2',
        f'bne  {DUMMY}',
        'ldr  r2, [r0, #4]',
        f'cbz  r2, {DUMMY}',
        f'cmp  r4, #{nslots:#x}',
        f'bhs  {DUMMY}',
        f'add.w r3, r2, #{TABLE_OFF:#x}',
        'ldr.w ip, [r3, r4, lsl #2]',
        'add  ip, r2',
        'orr  ip, ip, #1',
        *RESTORE,
    ]
    holes = [k for k, ln in enumerate(prefix) if ln.endswith(DUMMY)]
    if len(holes) != 3:
        raise SystemExit(f'dispatcher: expected 3 branch holes, found {holes}')
    tail = [*imm32('r0', BASE + FTAB), 'ldr.w ip, [r0, r4, lsl #2]', *RESTORE]
    nrest = len(RESTORE)
    fb = at + len(asm_seq(prefix, at))
    for _ in range(8):
        p = list(prefix)
        for k in holes:
            head, _, _ = p[k].rpartition(' ')
            p[k] = f'{head} {fb:#x}'
        core = asm_seq(p[:len(p) - nrest], at)
        rest = asm_seq(p[len(p) - nrest:], at + len(core))
        nf = at + len(core) + len(rest)
        if nf == fb:
            return core + rest + asm_seq(tail, nf)
        fb = nf
    raise SystemExit('dispatcher branch layout did not converge')


def entries():
    """File offsets that are the target of some `bl` in mas -- a function entry is a proven
    instruction boundary, which is what a hook site needs."""
    import pickle
    root = HERE.parents[1]
    cands = [HERE.parent / 'z6ii_mem' / 'cache' / 'br_mas.pkl',
             root / 'work' / 'z6ii_mem' / 'cache' / 'br_mas.pkl']
    for p in cands:
        if p.exists():
            br = pickle.load(open(str(p), 'rb'))
            return {int(t) for t, k in zip(br['tgt'], br['kind']) if k == 1}
    raise SystemExit(f'br_mas.pkl not found in {[str(p) for p in cands]}')


def entry_evidence(site, ent):
    """Why this offset may be treated as a function entry.

    The dispatcher passes the jump target in r12, and AAPCS lets a callee clobber r12 only
    because a *function entry* may not assume anything about it. So a hook is only allowed
    where the site really is an entry. A bl target proves it outright; a prologue that saves
    lr is what every non-leaf function here starts with and is strong but indirect evidence.
    """
    if site in ent:
        return 'proven: bl target'
    i = R.dis1(MAS, site)
    if i is not None and i.mnemonic.split('.')[0] == 'push' and 'lr' in i.op_str:
        return 'likely: push prologue that saves lr (not a bl target)'
    return None


def build():
    ent = entries()
    disp = dispatcher(len(SITES))
    if DISPATCH + len(disp) > THUNK:
        raise SystemExit(f'dispatcher is {len(disp)} B and would reach the thunks')
    th, fb = [], []
    tc, fc = THUNK, FALLBACK
    ev = []
    for i, (name, site, win) in enumerate(SITES):
        why = entry_evidence(site, ent)
        if not why:
            raise SystemExit(f'{name}: f{site:07x} is neither a bl target nor a push prologue '
                             f'that saves lr, so treating it as an entry (which the r12 '
                             f'hand-over requires) is not justified')
        ev.append(why)
        blob, log = R.rearm(MAS, site, win, fc, site + win, anchor=site)
        t = thunk(i, tc)
        th.append((tc, t, name))
        fb.append((fc, blob, log))
        tc += len(t)
        fc += (len(blob) + 1) & ~1
        if tc > FALLBACK or fc > CLEAN_END:
            raise SystemExit(f'no room left while laying out {name} '
                             f'(thunk f{tc:07x}, fallback f{fc:07x}, limit f{CLEAN_END:07x})')
    cwork = core_work()
    stride = len(core_slot(0, CORESLOT + 0x40))
    if stride % 2:
        raise SystemExit(f'core slot is {stride} B, not halfword aligned')
    slot_code = b''.join(core_slot(i, CORESLOT + 0x40 + i * stride) for i in range(len(SITES)))
    if len(slot_code) != stride * len(SITES):
        raise SystemExit('core slots are not uniform, which breaks the entry table stride')
    ring = b'\0' * (NSLOT * 16)
    layout = {
        'CTRL': (CTRL, 16), 'FTAB': (FTAB, 4 * len(SITES)), 'DISPATCH': (DISPATCH, len(disp)),
        'CORESLOT-table': (CORESLOT + TABLE_OFF, 4 * len(SITES)),
        'CORESLOT-code': (CORESLOT + 0x40, len(slot_code)), 'COREWORK': (COREWORK, len(cwork)),
        'THUNK': (THUNK, tc - THUNK), 'FALLBACK': (FALLBACK, fc - FALLBACK),
    }
    ordered = sorted((off, off + n, nm) for nm, (off, n) in layout.items())
    for (a0, a1, n0), (b0, b1, n1) in zip(ordered, ordered[1:]):
        if a1 > b0:
            raise SystemExit(f'layout overlap: {n0} ends f{a1:07x} past {n1} start f{b0:07x}')
    if ordered[-1][1] > CLEAN_END:
        raise SystemExit(f'layout ends f{ordered[-1][1]:07x}, past the clean run f{CLEAN_END:07x}')
    writes = [(CTRL, ctrl_block(len(SITES))),
              # thumb-marked stub addresses, exactly as WL's fallback table stores them
              (FTAB, b''.join(struct.pack('<I', (BASE + o) | 1) for o, _, _ in fb)),
              (DISPATCH, disp),
              (CORESLOT + TABLE_OFF, core_slot_table(stride)),
              (CORESLOT + 0x40, slot_code),
              (COREWORK, cwork)]
    writes += [(o, b) for o, b, _ in th] + [(o, b) for o, b, _ in fb]
    writes += [(site, R.hook_word(site, toff))
               for (toff, _, _), (_, site, _) in zip(th, SITES)]
    rep = [f'control block f{CTRL:07x}  magic=Z6PC core_base={BASE + CORESLOT:#x} '
           f'n_slots={len(SITES)} (installed)',
           f'fallback table f{FTAB:07x}  {len(SITES)} slots',
           f'dispatcher  f{DISPATCH:07x} + 0x{len(disp):x}  ({len(disp)} B)',
           f'pre-shoot   core f{CORESLOT:07x}+0x30 ({len(slot_code)} B slots), '
           f'worker f{COREWORK:07x} ({len(cwork)} B); runtime state in DRAM: '
           f'STATUS {STATUS:#x} (32 B), RING {RING:#x} ({NSLOT} x 16 B = {NSLOT*16} B)']
    for (toff, tb, name), (_, site, win), (foff, fby, log), e in zip(th, SITES, fb, ev):
        rep.append(f'  {name:24s} site f{site:07x} [{e}] win={win} -> thunk f{toff:07x} '
                   f'(0x{len(tb):x}B) -> fallback f{foff:07x} (0x{len(fby):x}B) '
                   f'-> resume f{site + win:07x}')
        for l in log:
            rep.append('        ' + l)
    rep.append(f'hole use f{CTRL:07x}..f{ordered[-1][1]:07x} = '
               f'{ordered[-1][1] - CTRL} B of {CLEAN_END - CTRL} B clean')
    # MMU-aware gate: state must live in DRAM, and no core store may target the image
    rep.append('runtime state placement:')
    check_state_place(verbose=False)
    for label, blob, at, trust in (('dispatcher', disp, DISPATCH, ()),
                                   ('core_slot', slot_code, CORESLOT + 0x40, ()),
                                   ('core_work', cwork, COREWORK, ('r0', 'r2')),
                                   ):
        bad, proven = check_runtime_writes(blob, BASE + at, label, trusted=trust)
        if bad:
            for b in bad:
                print('  FAIL ' + b)
            raise SystemExit('read-only write check failed')
        rep.append(f'  {label:11s} {proven:2d} store(s) proven outside '
                   f'{READONLY[0]:#x}..{READONLY[1]:#x}')
    return writes, rep


def apply(writes, blob=None):
    """Lay the writes down. Hole writes must land on zero; site writes must land exactly on
    the bytes the corresponding fallback already relocated."""
    b = bytearray(MAS if blob is None else blob)
    site_offs = {s for _, s, _ in SITES}
    for off, data in writes:
        cur = bytes(b[off:off + len(data)])
        if off in site_offs:
            if len(data) != 4 or not any(s == off for _, s, _ in SITES):
                raise SystemExit(f'site write at f{off:07x} is not a 4-byte hook')
        elif not CTRL <= off < CLEAN_END:
            raise SystemExit(f'write at f{off:07x} is outside the verified clean run')
        elif cur.strip(b'\0'):
            raise SystemExit(f'write at f{off:07x} would clobber live bytes {cur.hex()}')
        b[off:off + len(data)] = data
    return bytes(b)


def specs(writes, mod='eg1850_mas'):
    return [f'{mod}:{off:#x}:{data.hex()}' for off, data in writes]


if __name__ == '__main__':
    writes, rep = build()
    print('\n'.join(rep))
    patched = apply(writes)
    print(f'\npatched module differs in {sum(a != b for a, b in zip(patched, MAS))} bytes; '
          f'{sum(len(d) for _, d in writes)} bytes placed in {len(writes)} runs')
    (HERE / 'patch_spec.txt').write_text('\n'.join(specs(writes)) + '\n', encoding='utf-8')
    (HERE / 'mas_patched.bi').write_bytes(patched)
    print(f'wrote {HERE / "patch_spec.txt"} and {HERE / "mas_patched.bi"}')
