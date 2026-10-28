"""Rebuild the shutter-beep hook with the argument clobber fixed.

The shipped stub at VA 0x1002d884 does `mov r2, sp` and *then* runs the
original `bl 0x1073eafe` it displaced, so the displaced routine receives a
scratch-pointer third argument instead of the caller's. cpu0 then parks at
0x101034fc, the firmware's "no handler serviced this exception" spin.

Fix: call the original routine first with untouched registers, and hand the
scratch pointer to the added routine only. Because Thumb branches are
PC-relative, moving them means re-encoding, so this file carries a T4/T5
branch encoder and re-targets every moved branch; the result is checked by
disassembly before anything is written.

  noop2   redirect + original call, no added call  -> must match stock
  beep2   redirect + original call + beep call     -> the actual feature

  python work/beep_fix/make_variants.py
"""
import binascii
import pathlib
import struct

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

ROOT = pathlib.Path(__file__).resolve().parents[2]
STOCK = ROOT / "fw_verified" / "eg1850_mas_01700"
BROKEN = ROOT / "dist" / "beep" / "EG185070.BIN"
MASTER_SIZE = 38750138
STUB_OFF = 0xd884          # injected stub, VA 0x1002d884
REDIRECT_OFF = 0x71ecf8    # displaced bl, VA 0x1073ecf8
MAS_BASE = 0x10020000
MASK25 = (1 << 25) - 1


def sign(value, bits):
    top = 1 << (bits - 1)
    return (value ^ top) - top


def branch_decode(hw1, hw2, addr):
    """Offset of a B.W (T4) / BL.W (T5) pair, per the ARMv7 encoding."""
    s = (hw1 >> 10) & 1
    imm10 = hw1 & 0x3FF
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    imm11 = hw2 & 0x7FF
    i1 = 1 ^ j1 ^ s
    i2 = 1 ^ j2 ^ s
    raw = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    return addr + 4 + sign(raw, 25)


def branch_encode(target, addr, link):
    """Inverse of branch_decode; verified against the shipped stub."""
    offset = target - (addr + 4)
    if abs(offset) >= (1 << 23):
        raise ValueError("branch out of range")
    s = 1 if offset < 0 else 0
    raw = offset & MASK25
    i1 = (raw >> 23) & 1
    i2 = (raw >> 22) & 1
    imm10 = (raw >> 12) & 0x3FF
    imm11 = (raw >> 1) & 0x7FF
    j1 = 1 ^ i1 ^ s
    j2 = 1 ^ i2 ^ s
    hw1 = 0xF000 | (s << 10) | imm10
    if link:
        hw2 = 0xD000 | (j1 << 13) | (j2 << 11) | imm11
    else:
        hw2 = 0x9000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def bl(target, addr):
    return branch_encode(target, addr, True)


def b(target, addr):
    return branch_encode(target, addr, False)


NOP16 = bytes.fromhex("00bf")
MOV_R2_SP = bytes.fromhex("6a46")
PUSH_REGS = bytes.fromhex("2de90f50")
MRS = bytes.fromhex("eff30084")
PUSH_R45 = bytes.fromhex("30b4")
SUB_SP = bytes.fromhex("82b0")
ADD_SP = bytes.fromhex("02b0")
POP_R45 = bytes.fromhex("30bc")
MSR = bytes.fromhex("84f3008c")
POP_REGS = bytes.fromhex("bde80f50")


def read_targets(blob):
    """Original call, added call and return target, straight from the build."""
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    found = {}
    for insn in md.disasm(blob[STUB_OFF:STUB_OFF + 44], MAS_BASE + STUB_OFF):
        if len(insn.bytes) != 4 or insn.mnemonic not in ("bl", "b.w", "b"):
            continue
        hw = struct.unpack("<HH", insn.bytes)
        target = branch_decode(hw[0], hw[1], insn.address)
        found.setdefault(insn.mnemonic, []).append((insn.address, target))
    return found


def build_stub(orig_target, beep_target, return_target, with_beep):
    """Assemble the stub, keeping every branch pointed at the original target."""
    out = bytearray()
    pieces = [PUSH_REGS, MRS, PUSH_R45, SUB_SP]
    for piece in pieces:
        out += piece
    out += bl(orig_target, MAS_BASE + STUB_OFF + len(out))
    if with_beep:
        out += MOV_R2_SP
        out += bl(beep_target, MAS_BASE + STUB_OFF + len(out))
    out += ADD_SP + POP_R45 + MSR + POP_REGS
    out += b(return_target, MAS_BASE + STUB_OFF + len(out))
    return bytes(out)


def reseal(data):
    out = bytearray(data)
    struct.pack_into(">H", out, len(out) - 2, binascii.crc_hqx(bytes(out[:-2]), 0))
    return bytes(out)


def variant(with_beep):
    blob = BROKEN.read_bytes()
    targets = read_targets(blob)
    calls = targets["bl"]
    redirect = targets.get("b.w") or targets.get("b")
    orig_target = calls[0][1]
    beep_target = calls[1][1] if with_beep and len(calls) > 1 else None
    return_target = redirect[0][1]
    stub = build_stub(orig_target, beep_target, return_target, with_beep)
    data = bytearray(STOCK.read_bytes())
    data[STUB_OFF:STUB_OFF + len(stub)] = stub
    data[REDIRECT_OFF:REDIRECT_OFF + 4] = blob[REDIRECT_OFF:REDIRECT_OFF + 4]
    return bytes(reseal(data)), stub, (orig_target, beep_target, return_target)


def show(path):
    blob = path.read_bytes()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    print("--- %s ---" % path.name)
    for insn in md.disasm(blob[STUB_OFF:STUB_OFF + 44], MAS_BASE + STUB_OFF + 1):
        print("  0x%08x: %-8s %s %s" % (insn.address & ~1, insn.bytes.hex(),
                                        insn.mnemonic, insn.op_str))


def probes():
    """Bisect set: which part of the shipped stub actually breaks the boot.

    Measured on nikon-z62-dual-lab at equal guest time (uploads by guest 3.5s):
    stock 316, probeA/B/C ~346, probeD 12 + cpu0 parked at 0x101034fc. The only
    thing probeD adds over probeC is the APSR save/restore, so
    `mrs r4, apsr` / `msr apsr_nzcvqg, r4` is the fault, not the stack change.
    """
    blob = BROKEN.read_bytes()
    targets = read_targets(blob)
    orig = targets["bl"][0][1]
    beep = targets["bl"][1][1] if len(targets.get("bl", [])) > 1 else None
    ret = (targets.get("b.w") or targets.get("b"))[0][1]
    redirect = blob[REDIRECT_OFF:REDIRECT_OFF + 4]
    a0 = MAS_BASE + STUB_OFF

    def assemble(parts):
        stub = b""
        for part in parts:
            stub += part(a0 + len(stub)) if callable(part) else part
        return stub

    sets = {
        "probeA": [lambda a: bl(orig, a), lambda a: b(ret, a)],
        "probeB": [PUSH_REGS, lambda a: bl(orig, a), POP_REGS,
                   lambda a: b(ret, a)],
        "probeC": [PUSH_REGS, SUB_SP, lambda a: bl(orig, a), ADD_SP, POP_REGS,
                   lambda a: b(ret, a)],
        "probeD": [PUSH_REGS, MRS, lambda a: bl(orig, a), MSR, POP_REGS,
                   lambda a: b(ret, a)],
        "beep4": [PUSH_REGS, SUB_SP, lambda a: bl(orig, a), MOV_R2_SP,
                  lambda a: bl(beep, a), ADD_SP, POP_REGS,
                  lambda a: b(ret, a)],
    }
    stock = STOCK.read_bytes()
    for name, parts in sets.items():
        stub = assemble(parts)
        data = bytearray(stock)
        data[STUB_OFF:STUB_OFF + len(stub)] = stub
        data[REDIRECT_OFF:REDIRECT_OFF + 4] = redirect
        out = ROOT / "dist" / name / "EG185070.BIN"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(reseal(bytes(data)))
        print("wrote %s (stub %d bytes)" % (out.relative_to(ROOT), len(stub)))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", action="store_true",
                    help="emit the bisect set instead of the feature variants")
    args = ap.parse_args()
    if args.probes:
        probes()
        return
    assert STOCK.stat().st_size == MASTER_SIZE
    for name, with_beep in (("noop2", False), ("beep2", True)):
        data, stub, targets = variant(with_beep)
        out = ROOT / "dist" / name / "EG185070.BIN"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        diff = sum(1 for a, b in zip(data, STOCK.read_bytes()) if a != b)
        print("wrote %s: stub=%d bytes, %d bytes differ from stock; "
              "targets orig=%#x beep=%s return=%#x"
              % (out.relative_to(ROOT), len(stub), diff, targets[0],
                 hex(targets[1]) if targets[1] else "-", targets[2]))
        show(out)


if __name__ == "__main__":
    main()
