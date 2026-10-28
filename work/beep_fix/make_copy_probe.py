"""Find who copies the 0x44 status block, by watching the copy itself.

The status word is not stored directly: an A32 32-byte-at-a-time memcpy at
0x1035d1ec writes it, which is why no immediate exists and why a RAM watch
could not be used. The parameter table entry for that word has been retargeted
at the lab log port (see dist/statuswriter), so the copy arrives with
r0 == 0x042f0000.

The hook and the stub are ARM code, because 0x1035d1ec is A32 (it begins with
`push {r4,lr}` = E92D4010); a Thumb branch there would decode as garbage. Only
matching copies are logged: source pointer, length, and caller LR.

  python work/beep_fix/make_copy_probe.py
"""
import pathlib
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "work" / "beep_fix"))
import make_variants as mv

MEMCPY_VA = 0x1035d1ec
LOG_PORT = 0x042F0000
STUB_VA = mv.MAS_BASE + mv.STUB_OFF
STUB_VA = (STUB_VA + 3) & ~3          # ARM code must be word aligned

COND_AL, COND_NE = 0xE, 0x1


def movw(rd, imm16):
    return struct.pack("<I", 0xE3000000 | (((imm16 >> 12) & 0xF) << 16) |
                       (rd << 12) | (imm16 & 0xFFF))


def movt(rd, imm16):
    return struct.pack("<I", 0xE3400000 | (((imm16 >> 12) & 0xF) << 16) |
                       (rd << 12) | (imm16 & 0xFFF))


def arm_b(cond, target, insn_va):
    offset = (target - (insn_va + 8)) >> 2
    assert -0x800000 <= offset < 0x800000, offset
    return struct.pack("<I", ((cond << 28) | 0x0A000000) | (offset & 0xFFFFFF))


def build_stub():
    body = bytearray()
    body += struct.pack("<I", 0xE92D500F)          # push {r0-r3, ip, lr}
    body += movw(3, LOG_PORT & 0xFFFF) + movt(3, LOG_PORT >> 16)
    body += struct.pack("<I", 0xE1500003)          # cmp r0, r3
    bne_at = STUB_VA + len(body)
    body += b"\x00\x00\x00\x00"                    # patched below
    body += struct.pack("<I", 0xE5831000)          # str r1, [r3]  source
    body += struct.pack("<I", 0xE5832000)          # str r2, [r3]  length
    body += struct.pack("<I", 0xE583E000)          # str lr, [r3]  caller
    skip_at = STUB_VA + len(body)
    body[bne_at - STUB_VA:bne_at - STUB_VA + 4] = arm_b(COND_NE, skip_at, bne_at)
    body += struct.pack("<I", 0xE8BD500F)          # pop {r0-r3, ip, lr}
    body += struct.pack("<I", 0xE92D4010)          # displaced push {r4, lr}
    body += arm_b(COND_AL, MEMCPY_VA + 4, STUB_VA + len(body))
    return bytes(body)


def main():
    stub = build_stub()
    assert len(stub) % 4 == 0
    stock = bytearray(mv.STOCK.read_bytes())
    entry = 0x11913e5c - mv.MAS_BASE
    struct.pack_into("<I", stock, entry, LOG_PORT)      # status word -> log port
    stock[mv.STUB_OFF:mv.STUB_OFF + len(stub)] = stub
    off = MEMCPY_VA - mv.MAS_BASE
    stock[off:off + 4] = arm_b(COND_AL, STUB_VA, MEMCPY_VA)
    data = mv.reseal(bytes(stock))
    out = ROOT / "dist" / "copyprobe" / "EG185070.BIN"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    print("A32 stub %d bytes at 0x%08x" % (len(stub), STUB_VA))
    for insn in md.disasm(stub, STUB_VA):
        print("  0x%08x: %-8s %s %s" % (insn.address, insn.bytes.hex(),
                                        insn.mnemonic, insn.op_str))
    print("hook 0x%08x: %s | table entry: %s"
          % (MEMCPY_VA, data[off:off + 4].hex(), data[entry:entry + 4].hex()))
    diff = sum(1 for a, b in zip(data, mv.STOCK.read_bytes()) if a != b)
    print("wrote %s (%d bytes differ)" % (out.relative_to(ROOT), diff))


if __name__ == "__main__":
    main()
