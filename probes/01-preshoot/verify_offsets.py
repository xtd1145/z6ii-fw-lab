#!/usr/bin/env python
"""verify_offsets.py -- re-reads every offset cited in FINDINGS.md straight from
the raw images and asserts the expected bytes are there.
Read-only. Run after any re-decrypt to catch falsified citations.
"""
MAS = "D:/z6ii_fw_lab/extracted/eg1850_mas_01700"
Z8 = "D:/z6ii_fw_lab/all_modules/Z_8_0311/eg2070_0311b0.bi"

CHECKS_MAS = [
    (0x0037a33c, "CSEQEV01"), (0x0037a348, "CSEQEV02"), (0x0037a354, "CSEQAL01"),
    (0x0037a368, "CSEQAL02"), (0x0037a378, "CSEQAL03"), (0x0037a388, "CSEQFL01"),
    (0x0037a394, "CSEQMX01"), (0x0037a3a0, "CSEQMX02"), (0x0037a3b8, "capseq"),
    (0x0037c758, "A:\\CAPSEQ.BIN"), (0x0037b4d8, "MechaDebug"),
    (0x002061fc, "BufRelAl"), (0x00208c7c, "CapStQue"),
    (0x0242c53c, "CaptureStateCont"), (0x0242c5cc, "CaptureService"),
    (0x0242c6bc, "CaptureSequenceS"), (0x0242c8cc, "ImageBufferServi"),
    (0x0242cefb, "CBufferService"), (0x0242cf13, "CPipelineService"),
    (0x0242e730, "CAPSVC"), (0x0242e74c, "RCNT"), (0x0242e768, "BLKOUT"),
    (0x0242ed50, "CAPSEQ"), (0x0242ed6c, "CAPTG"), (0x0242ed88, "SHUTTER"),
    (0x0090fe30, "NotUse"),
]
CHECKS_Z8 = [
    (0x04ce4f08, "CSEQEV01"), (0x04ce5006, "CSQDFL01"), (0x04ce502a, "CSQDMX01"),
    (0x04ce4f8f, "capseq"), (0x04ce4fbd, "CAPSEQ.BIN"),
    (0x04cce4d5, "SHTSND"), (0x04cce4f1, "HSFCPROC"), (0x04cceabd, "CAPLV"),
    (0x04cd5dd8, "BufRelAl"),
]
# UTF-16LE citations (menu caption pools)
CHECKS_Z8_U16 = [
    (0x031568da, "C60"), (0x031568e0, "Pre-release burst"),
    (0x0315690a, "Post-release burst"), (0x03156936, "High fps viewfinder display"),
    (0x033a489c, "Pre-release Burst"), (0x033a48c8, "Post-release Burst"),
]


def main():
    mas = open(MAS, "rb").read()
    z8 = open(Z8, "rb").read()
    bad = 0
    for img, blob, checks, u16 in (("mas", mas, CHECKS_MAS, False),
                                   ("z8", z8, CHECKS_Z8, False),
                                   ("z8", z8, CHECKS_Z8_U16, True)):
        for off, exp in checks:
            enc = exp.encode("utf-16-le") if u16 else exp.encode("ascii")
            got = blob[off:off + len(enc)]
            ok = got == enc
            bad += 0 if ok else 1
            print("%-4s 0x%08x %-28s %s" % (img, off, exp, "OK" if ok else "*** MISMATCH: %r" % got))
    # absence assertions -- these are the verdict
    ABSENT_IN_MAS = [b"CSQDFL01", b"CSQDMX01", b"HSFCPROC", b"CAPLV", b"SHTSND",
                     b"CSEQEV03", b"CSEQAL04", b"CSEQMX03", b"CapabilityDataTable.c",
                     b"CaptureSequenceService.c", b"ShutterControl.c",
                     "Pre-release".encode("utf-16-le"),
                     "Post-release".encode("utf-16-le"),
                     "C60".encode("utf-16-le"), "C30".encode("utf-16-le"),
                     "C120".encode("utf-16-le")]
    for p in ABSENT_IN_MAS:
        n = mas.count(p)
        bad += 1 if n else 0
        print("mas absence %-30r count=%d %s" % (p, n, "OK" if n == 0 else "*** PRESENT"))
    print("\nFAILURES:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
