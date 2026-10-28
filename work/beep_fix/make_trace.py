"""Build a firmware image whose queue-API entries report channel + caller.

The RTOS queue wrappers at 0x10524004 (get) and 0x10524016 (request/wait) are
thin shims over 0x1035d37a/0x1035d3be with a global object. Hooking their
entries gives every queue use in the firmware, with the channel in r0 and the
caller in LR, without patching 43 call sites.

The hook is the shape probeB/probeC proved safe: push, do the logging, pop, then
run the displaced instructions and continue. It never touches APSR (that is what
crashed the shipped beep patch) and it returns to the original instruction
stream, so the UI path is untouched.

Reports go to the machine's lab log port at 0x042F0000, which the QEMU machine
prints as "z62.qlog <value>". Two lines per call: channel, then caller LR.

  python work/beep_fix/make_trace.py
"""
import binascii
import pathlib
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "work" / "beep_fix"))
import make_variants as mv  # branch encoder + reseal + stock path

MAS_BASE = mv.MAS_BASE
STUB_OFF = mv.STUB_OFF
LOG_PORT = 0x042F0000
# (entry VA, displaced bytes, continue VA, expected displaced emulation)
HOOKS = [
    # 0x10524004: mov r1,r0 ; ldr r0,[pc,#0x24] -> 0xc1819b1c ; b.w queue_get
    dict(entry=0x10524004, displaced=bytes.fromhex("0146"),
         cont=0x10524008, emulate="object"),
    # 0x10524016: mov r3,r2 ; mov r2,r1 ; mov r1,r0 ; ldr r0,=obj ; b.w queue_req
    dict(entry=0x10524016, displaced=bytes.fromhex("13460a46"),
         cont=0x1052401a, emulate=""),
]

PUSH = bytes.fromhex("2de90f50")
POP = bytes.fromhex("bde80f50")


def movw(rd, imm16):
    i = (imm16 >> 11) & 1
    hw1 = 0xF240 | (i << 10) | ((imm16 >> 12) & 0xF)
    hw2 = (((imm16 >> 8) & 7) << 12) | (rd << 8) | (imm16 & 0xFF)
    return struct.pack("<HH", hw1, hw2)


def movt(rd, imm16):
    i = (imm16 >> 11) & 1
    hw1 = 0xF2C0 | (i << 10) | ((imm16 >> 12) & 0xF)
    hw2 = (((imm16 >> 8) & 7) << 12) | (rd << 8) | (imm16 & 0xFF)
    return struct.pack("<HH", hw1, hw2)


def selfcheck():
    """The encoders must reproduce bytes the firmware already contains."""
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    cases = [(movw(1, 0x0410), "movw r1, #0x410"),
             (movw(3, 0x12c0), "movw r3, #0x12c0"),
             (movw(2, 0xF000), "movw r2, #0xf000"),
             (movt(2, 0x042F), "movt r2, #0x42f"),
             (movw(0, 0x9B1C), "movw r0, #0x9b1c"),
             (movt(0, 0xC181), "movt r0, #0xc181")]
    for enc, want in cases:
        got = next(md.disasm(enc, 0))
        text = "%s %s" % (got.mnemonic, got.op_str)
        assert text == want, "encoder mismatch: %r != %r" % (text, want)
    print("encoder self-check: %d/%d ok" % (len(cases), len(cases)))


def build_stub(hook, addr):
    body = bytearray()
    body += PUSH
    body += movw(2, LOG_PORT & 0xFFFF)
    body += movt(2, LOG_PORT >> 16)
    body += bytes.fromhex("1060")        # str r0, [r2]  channel
    body += bytes.fromhex("c2f800e0")    # str.w lr, [r2]  caller (r14 needs T2)
    body += POP
    body += hook["displaced"]
    if hook["emulate"] == "object":
        # The displaced `ldr r0,[pc,#0x24]` loaded the queue object; the stub
        # sits elsewhere, so materialise the same constant directly.
        body += movw(0, 0x9B1C) + movt(0, 0xC181)
    body += mv.b(hook["cont"], addr + len(body))
    return bytes(body)


def main():
    selfcheck()
    stock = bytearray(mv.STOCK.read_bytes())
    cursor = STUB_OFF
    report = []
    for hook in HOOKS:
        stub_va = MAS_BASE + cursor
        stub = build_stub(hook, stub_va)
        stock[cursor:cursor + len(stub)] = stub
        # Redirect the entry: b.w stub (4 bytes) over the displaced pair.
        entry_off = hook["entry"] - MAS_BASE
        stock[entry_off:entry_off + 4] = mv.b(stub_va, hook["entry"])
        report.append((hook, stub_va, len(stub)))
        cursor += len(stub) + 8
    data = mv.reseal(bytes(stock))
    out = ROOT / "dist" / "tracequeue" / "EG185070.BIN"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    total = sum(1 for a, b in zip(data, mv.STOCK.read_bytes()) if a != b)
    print("wrote %s (%d bytes differ)" % (out.relative_to(ROOT), total))
    for hook, stub_va, length in report:
        print("\n--- hook at 0x%08x -> stub 0x%08x (%d bytes) ---"
              % (hook["entry"], stub_va, length))
        off = stub_va - MAS_BASE
        for insn in md.disasm(data[off:off + length], stub_va | 1):
            print("  0x%08x: %-8s %s %s" % (insn.address & ~1,
                                            insn.bytes.hex(),
                                            insn.mnemonic, insn.op_str))


if __name__ == "__main__":
    main()
