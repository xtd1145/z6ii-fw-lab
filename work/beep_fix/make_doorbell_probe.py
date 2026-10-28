"""Hook the notification broadcaster and log its report block.

The status codes in this firmware are not stored directly: the broadcaster at
0x1026d6bc builds a report from 0xc23d9728 and rings the doorbell 0x04200181,
whose event index is what the subscriber maps to a status code (14 -> 0x44 in
the Z72 table). The index is data, not an immediate, so the only way to see it
is to log the block at the moment it is rung.

The stub writes five words per call to the machine's lab log port at 0x042f0000
(reads come back as "z62.qlog <value>" lines): report+0x00, +0x04, +0x08, +0x0c
and +0x64. Same push/pop shape as probeB, which soaked 10/10 without disturbing
the UI; no APSR access.

  python work/beep_fix/make_doorbell_probe.py
"""
import binascii
import pathlib
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "work" / "beep_fix"))
import make_variants as mv

HOOK_VA = 0x1026d6bc          # broadcaster entry
DISPLACED_LEN = 4             # ldr r1,[pc,#0xbc] -> 0xc23d9728
CONTINUE_VA = HOOK_VA + DISPLACED_LEN
REPORT = 0xc23d9728
LOG_PORT = 0x042F0000
WORDS = (0x00, 0x04, 0x08, 0x0c, 0x64)
STUB_VA = mv.MAS_BASE + mv.STUB_OFF


def movw(rd, imm16):
    i = (imm16 >> 11) & 1
    return struct.pack("<HH", 0xF240 | (i << 10) | ((imm16 >> 12) & 0xF),
                       (((imm16 >> 8) & 7) << 12) | (rd << 8) | (imm16 & 0xFF))


def movt(rd, imm16):
    i = (imm16 >> 11) & 1
    return struct.pack("<HH", 0xF2C0 | (i << 10) | ((imm16 >> 12) & 0xF),
                       (((imm16 >> 8) & 7) << 12) | (rd << 8) | (imm16 & 0xFF))


def build_stub():
    body = bytearray(mv.PUSH_REGS)
    body += movw(2, LOG_PORT & 0xFFFF) + movt(2, LOG_PORT >> 16)
    body += movw(3, REPORT & 0xFFFF) + movt(3, REPORT >> 16)
    for off in WORDS:
        if off:
            body += struct.pack("<HH", 0xF8D3, 0x0000 | off)   # ldr.w r0,[r3,#off]
        else:
            body += struct.pack("<H", 0x6818)                   # ldr r0,[r3]
        body += struct.pack("<H", 0x6010)                       # str r0,[r2]
    body += mv.POP_REGS
    body += movw(1, REPORT & 0xFFFF) + movt(1, REPORT >> 16)    # displaced load
    body += mv.b(CONTINUE_VA, STUB_VA + len(body))
    return bytes(body)


def main():
    stub = build_stub()
    stock = bytearray(mv.STOCK.read_bytes())
    stock[mv.STUB_OFF:mv.STUB_OFF + len(stub)] = stub
    off = HOOK_VA - mv.MAS_BASE
    stock[off:off + 4] = mv.b(STUB_VA, HOOK_VA)
    data = mv.reseal(bytes(stock))
    out = ROOT / "dist" / "doorbell" / "EG185070.BIN"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    print("stub %d bytes at 0x%08x" % (len(stub), STUB_VA))
    for insn in md.disasm(stub, STUB_VA | 1):
        print("  0x%08x: %-8s %s %s" % (insn.address & ~1, insn.bytes.hex(),
                                        insn.mnemonic, insn.op_str))
    print("hook at 0x%08x: %s" % (HOOK_VA, data[off:off + 4].hex()))
    diff = sum(1 for a, b in zip(data, mv.STOCK.read_bytes()) if a != b)
    print("wrote %s (%d bytes differ)" % (out.relative_to(ROOT), diff))


if __name__ == "__main__":
    main()
