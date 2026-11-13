#!/usr/bin/env python3
"""Targeted PC-relative xref sweep: report adr/add/ldr sites reaching exact VMAs."""
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM

BASE = 0x10000000
MAS = r"D:\z6ii_fw_lab\all_modules\Z6_2_0170\eg1850_mas_01700"

TARGETS = {
    0x11092D14: "menu:Release mode",
    0x11092D2C: "menu:Single frame",
    0x11092D58: "menu:Continuous H",
    0x11092D70: "menu:Continuous L",
    0x11092CD8: "menu:bundle start",
    0x107495B0: "log:InitiateCapture",
    0x10749578: "log:RegisterNotifyEvent",
    0x10749608: "log:Release was failed",
    0x1074B74C: "log:ShutterDrive",
    0x1074C214: "log:WaitReleasePermission",
    0x1074A0B4: "log:InspectReleaseFD",
    0x1074A138: "log:InspectReleaseLV",
    0x1208D5FC: "log:ExecuteAutoRelease",
    0x10918C00: "tbl:ID_SW0",
    0x10918FC4: "tbl:REL_HALF",
    0x10918FEC: "tbl:REL_ON",
    0x1242E66C: "tbl:CAPST",
    0x1242ED50: "tbl:CAPSEQ",
    0x1242ED6C: "tbl:CAPTG",
    0x1242ED88: "tbl:SHUTTER",
    0x10910558: "tbl:AFAssistLight",
    0x10910588: "tbl:ShutterDrive-op",
    0x10910898: "tbl:InspectReleaseFD-op",
}

CHUNK = 0x200000


def main():
    data = open(MAS, "rb").read()
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    md.skipdata = True
    out = open(sys.argv[1], "w", encoding="utf-8") if len(sys.argv) > 1 else sys.stdout
    hits = 0
    for off in range(0, len(data), CHUNK):
        end = min(off + CHUNK, len(data))
        for ins in md.disasm(data[off:end], BASE + off):
            if ins.id == 0:
                continue
            if ins.mnemonic not in ("adr", "add", "sub", "ldr"):
                continue
            try:
                ops = ins.operands
            except Exception:
                continue
            imm = None
            for op in ops:
                if op.type == ARM_OP_IMM:
                    imm = op.imm
            if imm is None:
                continue
            if ins.mnemonic == "ldr":
                cands = (((ins.address + 4) & ~3) + imm,)
            else:
                cands = (ins.address + 4 + imm, ins.address + 4 - imm)
            for cand in cands:
                tag = TARGETS.get(cand)
                if tag:
                    out.write("0x%08x\t%-26s\t%s %s\n" % (ins.address, tag,
                                                          ins.mnemonic, ins.op_str))
                    hits += 1
    if out is not sys.stdout:
        out.close()
        sys.stderr.write("hits=%d\n" % hits)


if __name__ == "__main__":
    main()
