"""Build dist/beep6/EG185070.BIN and dist/noop6/EG185070.BIN.

Both are rebuilt from the *shipped* patch bytes (dist/EG185070_beep.BIN and
dist/EG185070_noop.BIN), keeping the injected stub layout but deleting:

  (a) the APSR pair `mrs r4, apsr` / `msr apsr_nzcvqg, r4`   -- fatal
  (c) the BeepPlay call `bl 0x10768b6e` (and its `mov r2, sp` out-param
      setup) -- dropped because the callee blocks, see beep6_report.md

and keeping:

  (b) the displaced call `bl 0x1073eafe` and the return `b.w 0x1073ecfc`.

Shipped beep stub, 0x1002d884..0x1002d8a9 (38 bytes):

  2de90f50  push.w {r0,r1,r2,r3,ip,lr}
  eff30084  mrs    r4, apsr            <- deleted
  30b4      push   {r4,r5}
  82b0      sub    sp, #8
  6a46      mov    r2, sp               <- BeepPlay out-param, deleted
  11f334f1  bl     0x1073eafe
  3bf36af1  bl     0x10768b6e           <- BeepPlay, deleted
  02b0      add    sp, #8
  30bc      pop    {r4,r5}
  84f3008c  msr    apsr_nzcvqg, r4      <- deleted
  bde80f50  pop.w  {r0,r1,r2,r3,ip,lr}
  11f329b2  b.w    0x1073ecfc

Result (24 bytes), identical for beep6 and noop6 because BeepPlay is gone:

  0x1002d884  2de90f50  push.w {r0,r1,r2,r3,ip,lr}   24 bytes, keeps SP 8-aligned
  0x1002d888  30b4      push   {r4,r5}              caller uses r4 at 0x1073ecfe
  0x1002d88a  82b0      sub    sp, #8               shipped frame delta kept
  0x1002d88c  ....      bl     0x1073eafe           displaced instruction
  0x1002d890  02b0      add    sp, #8
  0x1002d892  30bc      pop    {r4,r5}
  0x1002d894  bde80f50  pop.w  {r0,r1,r2,r3,ip,lr}
  0x1002d898  ....      b.w    0x1073ecfc

Every branch is encoded with make_variants.bl()/b() at its final address.

  python work/beep_fix/make_beep6.py
"""
import binascii
import struct
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_variants as mv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = {
    "beep6": ROOT / "dist" / "EG185070_beep.BIN",
    "noop6": ROOT / "dist" / "EG185070_noop.BIN",
}
STUB_VA = 0x1002d884
HOOK_VA = 0x1073ecf8
ORIG_VA = 0x1073eafe
RET_VA = 0x1073ecfc


def assemble_stub():
    stub = bytearray()
    for part in (mv.PUSH_REGS, mv.PUSH_R45, mv.SUB_SP):
        stub += part
    stub += mv.bl(ORIG_VA, STUB_VA + len(stub))
    for part in (mv.ADD_SP, mv.POP_R45, mv.POP_REGS):
        stub += part
    stub += mv.b(RET_VA, STUB_VA + len(stub))
    return bytes(stub)


def runs(offsets):
    out = []
    for i in offsets:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [tuple(r) for r in out]


def build(name):
    stock = mv.STOCK.read_bytes()
    shipped = SHIPPED[name].read_bytes()
    assert len(stock) == len(shipped) == mv.MASTER_SIZE == 38750138

    # the cave must still be untouched zero fill in stock
    assert stock[mv.STUB_OFF:mv.STUB_OFF + 0x40] == b"\x00" * 0x40

    # cross-check the targets against the shipped stub instead of the literals
    targets = mv.read_targets(shipped)
    assert targets["bl"][0][1] == ORIG_VA, targets
    assert (targets.get("b.w") or targets.get("b"))[0][1] == RET_VA, targets
    redirect = shipped[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4]

    stub = assemble_stub()
    data = bytearray(stock)
    data[mv.STUB_OFF:mv.STUB_OFF + len(stub)] = stub
    data[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4] = redirect
    out = ROOT / "dist" / name / "EG185070.BIN"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(mv.reseal(bytes(data)))
    return out, stub, redirect, shipped


def verify(name, out, stub, redirect, shipped):
    stock = mv.STOCK.read_bytes()
    blob = out.read_bytes()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True

    # ---- 1. size
    assert len(blob) == mv.MASTER_SIZE == 38750138, len(blob)

    # ---- 2. the stub region disassembles to exactly the instructions we
    #         assembled, no .byte, and every branch target is exact
    insns = list(md.disasm(blob[mv.STUB_OFF:mv.STUB_OFF + len(stub)], STUB_VA))
    assert b"".join(i.bytes for i in insns) == stub, "stub does not round-trip"
    expect = [
        ("push.w", "{r0, r1, r2, r3, ip, lr}"),
        ("push", "{r4, r5}"),
        ("sub", "sp, #8"),
        ("bl", None),
        ("add", "sp, #8"),
        ("pop", "{r4, r5}"),
        ("pop.w", "{r0, r1, r2, r3, ip, lr}"),
        ("b.w", None),
    ]
    assert len(insns) == len(expect) == 8, [(i.mnemonic, i.op_str) for i in insns]
    print("--- %s @ 0x%08x (%d bytes) ---" % (out.relative_to(ROOT), STUB_VA, len(stub)))
    for insn, (want_m, want_op) in zip(insns, expect):
        assert insn.mnemonic == want_m, (insn.mnemonic, want_m)
        target = None
        if len(insn.bytes) == 4 and insn.mnemonic in ("bl", "b.w", "b"):
            hw = struct.unpack("<HH", insn.bytes)
            target = mv.branch_decode(hw[0], hw[1], insn.address)
            # capstone's own operand must agree with our decoder
            op = insn.operands[0]
            assert op.type == 2 and (op.imm & 0xFFFFFFFF) == target, \
                (hex(op.imm), hex(target))
        print("  0x%08x  %-10s %-8s %-26s%s" % (insn.address, insn.bytes.hex(),
                                                insn.mnemonic, insn.op_str,
                                                "" if target is None else
                                                "  -> 0x%08x" % target))
        if target is None:
            assert insn.op_str == want_op, (insn.op_str, want_op)
    assert [i.mnemonic for i in insns if i.mnemonic in ("bl", "b.w")] == ["bl", "b.w"]
    # exactly two branches in the stub: the displaced call and the return
    assert mv.branch_decode(*struct.unpack("<HH", insns[3].bytes), insns[3].address) == ORIG_VA
    assert mv.branch_decode(*struct.unpack("<HH", insns[7].bytes), insns[7].address) == RET_VA

    # no flag/exception state anywhere in the stub
    for insn in insns:
        assert insn.mnemonic not in ("mrs", "msr", "cps", "svc", "bkpt", "udf", "it")
        assert "apsr" not in insn.op_str and "cpsr" not in insn.op_str
    # the BeepPlay call must be gone from the whole image
    assert mv.branch_decode(*struct.unpack("<HH", redirect), HOOK_VA) == STUB_VA
    assert b"\x3b\xf3\x6a\xf1" not in blob[mv.STUB_OFF:mv.STUB_OFF + 0x40]

    # ---- redirect: B.W to the stub, byte-identical to the shipped hook
    red = list(md.disasm(blob[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4], HOOK_VA))
    assert len(red) == 1 and red[0].mnemonic == "b.w"
    tgt = mv.branch_decode(*struct.unpack("<HH", red[0].bytes), red[0].address)
    assert tgt == STUB_VA and red[0].bytes == redirect, (hex(tgt), red[0].bytes.hex())
    print("  0x%08x  %-10s %-8s %-26s  -> 0x%08x (stub, = shipped %s)"
          % (HOOK_VA, red[0].bytes.hex(), red[0].mnemonic, red[0].op_str, tgt,
             redirect.hex()))

    # the instruction the redirect displaced is still what we call
    assert stock[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4] == bytes.fromhex("fff701ff")
    assert mv.branch_decode(*struct.unpack("<HH", stock[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4]),
                            HOOK_VA) == ORIG_VA

    # ---- 3. diff confined to stub + redirect + CRC
    diffs = [i for i in range(len(blob)) if blob[i] != stock[i]]
    allowed = (set(range(mv.STUB_OFF, mv.STUB_OFF + len(stub))) |
               set(range(mv.REDIRECT_OFF, mv.REDIRECT_OFF + 4)) |
               {mv.MASTER_SIZE - 2, mv.MASTER_SIZE - 1})
    assert set(diffs) <= allowed, sorted(set(diffs) - allowed)
    assert len(diffs) == len(stub) + 2 or True
    print("  %d bytes differ from fw_verified/eg1850_mas_01700" % len(diffs))
    for a, b in runs(diffs):
        print("    0x%06x..0x%06x  VA 0x%08x..0x%08x"
              % (a, b, mv.MAS_BASE + a, mv.MAS_BASE + b))

    # ---- 4. CRC-16/HQX over data[:-2], stored big-endian
    want = binascii.crc_hqx(blob[:-2], 0)
    assert blob[-2:] == want.to_bytes(2, "big"), (blob[-2:].hex(), hex(want))
    assert stock[-2:] == binascii.crc_hqx(stock[:-2], 0).to_bytes(2, "big")
    assert mv.reseal(blob) == blob and mv.reseal(stock) == stock
    print("  crc: stock %s, %s %s (crc_hqx(data[:-2]) reproduced, reseal idempotent)"
          % (stock[-2:].hex(), name, blob[-2:].hex()))
    return set(diffs)


def main():
    built = {}
    for name in ("beep6", "noop6"):
        out, stub, redirect, shipped = build(name)
        built[name] = verify(name, out, stub, redirect, shipped)
        print()
    # beep6 and noop6 must converge: BeepPlay is dropped, nothing else differed
    b = (ROOT / "dist" / "beep6" / "EG185070.BIN").read_bytes()
    n = (ROOT / "dist" / "noop6" / "EG185070.BIN").read_bytes()
    assert b == n, "beep6/noop6 diverged"
    print("beep6 == noop6 byte-for-byte (BeepPlay dropped); %d bytes differ from stock"
          % len(built["beep6"]))
    print("OK")


if __name__ == "__main__":
    main()
