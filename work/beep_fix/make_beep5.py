"""Build dist/beep5/EG185070.BIN, the boot-safe shutter-hook variant.

Decision recorded in work/beep_fix/beep5_report.md: none of the four audio
routines in audio_path_report.md can be called with inputs that are provable
from static evidence, so beep5 is the hook-only fallback (call the original
0x1073eafe and return to 0x1073ecfc). No sound is added; the point of this
build is a patch that is bit-identical to stock apart from the hook and that
cannot fault.

Stub shape = probeB (verified boot-safe), no APSR/CPSR access at all:

  0x1002d884  2de9 0f50   push.w {r0,r1,r2,r3,ip,lr}      (4 bytes)
  0x1002d888  ....        bl     0x1073eafe    <- displaced instruction
  0x1002d88c  bde8 0f50   pop.w  {r0,r1,r2,r3,ip,lr}
  0x1002d890  ....        b.w    0x1073ecfc    <- original return address

Every branch is encoded with make_variants.bl()/b() at the address it will
finally occupy. The redirect at 0x1073ecf8 is re-encoded as a 32-bit B.W
(not BL): the shipped patch used B.W there, so stub entry keeps the caller's
lr, and b() reproduces the shipped bytes bit-for-bit.

  python work/beep_fix/make_beep5.py
"""
import struct
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_variants as mv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dist" / "beep5" / "EG185070.BIN"

STUB_VA = 0x1002d884          # zero cave, = MAS_BASE + STUB_OFF
HOOK_VA = 0x1073ecf8          # displaced `bl` in the render path
ORIG_VA = 0x1073eafe          # what the displaced bl called
RET_VA = 0x1073ecfc           # instruction after the displaced bl

EXPECTED = [
    ("push.w", "{r0, r1, r2, r3, ip, lr}"),
    ("bl", ORIG_VA),
    ("pop.w", "{r0, r1, r2, r3, ip, lr}"),
    ("b.w", RET_VA),
]


def runs(offsets):
    out = []
    for i in offsets:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [tuple(r) for r in out]


def main():
    stock = mv.STOCK.read_bytes()
    assert len(stock) == mv.MASTER_SIZE, len(stock)

    # 1. the cave we write into must be unused zero fill in stock
    cave = stock[mv.STUB_OFF:mv.STUB_OFF + 16]
    assert cave == b"\x00" * 16, cave.hex()

    # 2. cross-check both branch targets against the shipped patch instead of
    #    trusting the constants above
    shipped = mv.read_targets(mv.BROKEN.read_bytes())
    assert shipped["bl"][0][1] == ORIG_VA, shipped
    assert (shipped.get("b.w") or shipped.get("b"))[0][1] == RET_VA, shipped
    print("shipped patch targets: original=0x%08x return=0x%08x"
          % (shipped["bl"][0][1], (shipped.get("b.w") or shipped.get("b"))[0][1]))

    # 3. assemble at final addresses
    stub = bytearray()
    stub += mv.PUSH_REGS
    stub += mv.bl(ORIG_VA, STUB_VA + len(stub))
    stub += mv.POP_REGS
    stub += mv.b(RET_VA, STUB_VA + len(stub))
    redirect = mv.b(STUB_VA, HOOK_VA)   # B.W, as in the shipped hook

    shipped_redirect = mv.BROKEN.read_bytes()[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4]
    print("redirect 0x%08x: mine=%s shipped=%s (match=%s)"
          % (HOOK_VA, redirect.hex(), shipped_redirect.hex(),
             redirect == shipped_redirect))

    data = bytearray(stock)
    data[mv.STUB_OFF:mv.STUB_OFF + len(stub)] = stub
    data[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4] = redirect
    out = mv.reseal(bytes(data))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(out)

    # ---- verification -------------------------------------------------
    blob = OUT.read_bytes()
    assert len(blob) == mv.MASTER_SIZE == 38750138, len(blob)

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    insns = list(md.disasm(blob[mv.STUB_OFF:mv.STUB_OFF + len(stub)], STUB_VA))
    assert len(insns) == 4, [(i.mnemonic, i.op_str) for i in insns]
    print("\n--- dist/beep5/EG185070.BIN @ 0x%08x ---" % STUB_VA)
    for insn, (want_m, want_op) in zip(insns, EXPECTED):
        target = None
        if len(insn.bytes) == 4 and insn.mnemonic in ("bl", "b.w", "b"):
            hw = struct.unpack("<HH", insn.bytes)
            target = mv.branch_decode(hw[0], hw[1], insn.address)
        print("  0x%08x: %-8s %-8s %-22s"
              % (insn.address, insn.bytes.hex(), insn.mnemonic,
                 insn.op_str + ("   -> 0x%08x" % target if target else "")))
        assert insn.mnemonic == want_m, (insn.mnemonic, want_m)
        if target is None:
            assert insn.op_str == want_op, (insn.op_str, want_op)
        else:
            # exact target, not off by 2 or 4
            assert target == want_op, (hex(target), hex(want_op))
    assert len(stub) == 16

    # no flag/handler state is touched anywhere in the stub
    banned = ("mrs", "msr", "cps", "svc", "bkpt", "udf", "it")
    for insn in insns:
        assert insn.mnemonic not in banned, insn.mnemonic
        assert "apsr" not in insn.op_str and "cpsr" not in insn.op_str
    assert stub[0:2] == b"\x2d\xe9" and stub[2:4] == b"\x0f\x50"   # push.w reglist
    assert stub[8:10] == b"\xbd\xe8" and stub[10:12] == b"\x0f\x50"  # pop.w reglist

    # redirect really lands on the stub, and the hook site is the only change
    red = list(md.disasm(blob[mv.REDIRECT_OFF:mv.REDIRECT_OFF + 4], HOOK_VA))
    assert len(red) == 1 and red[0].mnemonic == "b.w"
    hw = struct.unpack("<HH", red[0].bytes)
    red_target = mv.branch_decode(hw[0], hw[1], red[0].address)
    assert red_target == STUB_VA, hex(red_target)
    print("  0x%08x: %s %s %s   -> 0x%08x (stub)"
          % (HOOK_VA, red[0].bytes.hex(), red[0].mnemonic, red[0].op_str,
             red_target))

    # byte diff vs stock: stub, redirect, trailing CRC only
    diffs = [i for i in range(len(blob)) if blob[i] != stock[i]]
    allowed = set(range(mv.STUB_OFF, mv.STUB_OFF + len(stub))) | \
        set(range(mv.REDIRECT_OFF, mv.REDIRECT_OFF + 4)) | \
        {mv.MASTER_SIZE - 2, mv.MASTER_SIZE - 1}
    assert set(diffs) <= allowed, sorted(set(diffs) - allowed)
    print("\n%d bytes differ from fw_verified/eg1850_mas_01700" % len(diffs))
    for a, b in runs(diffs):
        print("  0x%06x..0x%06x  VA 0x%08x..0x%08x  %s"
              % (a, b, mv.MAS_BASE + a, mv.MAS_BASE + b, blob[a:b + 1].hex()))

    # CRC trailer is the CRC-16/CCITT-FALSE-style hqx the firmware carries;
    # reseal() must be idempotent on our build and reproduce stock's trailer
    assert mv.reseal(blob) == blob
    assert mv.reseal(stock) == stock, "reseal() does not reproduce stock CRC"
    print("crc trailer stock=%s beep5=%s (reseal idempotent, stock reproduced)"
          % (stock[-2:].hex(), blob[-2:].hex()))
    print("\nOK: %s" % OUT)


if __name__ == "__main__":
    main()
