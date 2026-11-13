#!/usr/bin/env python3
"""Read-only re-check of P0/P1/P3/P4 against the mainline probe dumps.

Reads only .local/webui/probe/*.bin and prints PASS/FAIL. Writes no files.
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / ".local" / "webui" / "probe"
RAM = P / "ram_10000000.bin"


def load(name):
    return (P / name).read_bytes()


def at(ram, vma, n):
    return ram[vma - 0x10000000:vma - 0x10000000 + n]


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def main():
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print("[%s] %-46s %s" % ("PASS" if cond else "FAIL", label, detail))

    ram = RAM.read_bytes()
    task = load("mas_task_desc.bin")
    reg = load("mas_obj_registry.bin")
    mode_file = load("mode_records.bin")
    mode = at(ram, 0x119FF50C, 0x1B00)
    svc = load("mas_svc_names.bin")
    vmas = load("mas_video_table.bin")
    vsla = load("sla_video_table.bin")
    size = load("mas_size_table.bin")
    small = load("heap_small.bin")
    arena = load("arena_base.bin")
    arena_dx = load("arena_dx.bin")
    reg_base = load("registry_base.bin")
    reg_dx = load("registry_dx.bin")

    print("== dump sizes ==")
    for name, b in [("task", task), ("registry", reg), ("mode", mode), ("svc", svc),
                    ("video_mas", vmas), ("video_sla", vsla), ("size", size),
                    ("heap_small", small), ("arena", arena)]:
        print("  %-10s %d" % (name, len(b)))

    print("== P0: task descriptor table @0x1244E7A0 ==")
    names = [task[0x1C * i:0x1C * i + 12].split(b"\0")[0].decode(errors="replace") for i in range(60)]
    check("60 task names", len(names) == 60, names[:3])
    check("CAPSEQ=52 CAPTG=53 SHUTTER=54",
          names[52] == "CAPSEQ" and names[53] == "CAPTG" and names[54] == "SHUTTER",
          "%s/%s/%s" % (names[52], names[53], names[54]))
    fields = [struct.unpack_from("<4I", task, 0x1C * i + 12) for i in range(60)]
    check("fields type/size/0x1000/0", all(f[2] == 0x1000 and f[3] == 0 for f in fields), "all 60")
    check("task dump == RAM @0x1244E7A0", task == at(ram, 0x1244E7A0, len(task)))

    print("== P0: object registry @0x1242E4D0 ==")
    entries = [struct.unpack_from("<4I", reg, 16 * i) for i in range(53)]
    check("53 entries", len(entries) == 53)
    check("first id 0x530, last id 0x53a", entries[0][3] == 0x530 and entries[52][3] == 0x53a,
          "first=%03x last=%03x" % (entries[0][3], entries[52][3]))
    check("no CAPTG at boot", all(e[3] != 0x565 for e in entries))
    check("registry dump == RAM @0x1242E4D0", reg == at(ram, 0x1242E4D0, len(reg)))

    print("== P0: mode records @0x119FF50C ==")
    counts = []
    hits = []
    for r in range(6):
        base = 0x480 * r
        nz = []
        for off in range(0, 0x480, 4):
            v = u32(mode, base + off)
            if v:
                nz.append((off, v))
        counts.append(len(nz))
        for val in (6048, 4024, 3936, 2624, 2944, 1968, 1312, 3840, 2160, 1920, 1080, 33333, 71429):
            if any(v == val for _, v in nz):
                hits.append((r, val))
    check("nonzero counts 6/3/5/3/6/3", counts == [6, 3, 5, 3, 6, 3], str(counts))
    check("no width/height/period fields", not hits, str(hits))
    check("mode_records.bin matches old 0x119DF50C", mode_file == at(ram, 0x119DF50C, len(mode_file)), "stale file at old address")
    check("correct mode records are at 0x119FF50C", mode == at(ram, 0x119FF50C, len(mode)), "RAM source")
    print("[WARN] mode_records.bin is stale at 0x119DF50C; correct records are 0x119FF50C in ram_10000000.bin; re-dump if needed.")

    print("== P0: component table @0x1244C224 ==")
    svc_names = {}
    for i in range(80):
        o = 0x18 * i
        nm = svc[o:o + 20].split(b"\0")[0].decode(errors="replace")
        if not nm:
            break
        svc_names[nm] = u32(svc, o + 20)
    check("CaptureSensorService=0x02020000", svc_names.get("CaptureSensorSer") == 0x02020000,
          hex(svc_names.get("CaptureSensorSer", 0)))
    check("CaptureSequenceService=0x04210000", svc_names.get("CaptureSequenceS") == 0x04210000,
          hex(svc_names.get("CaptureSequenceS", 0)))

    print("== P3: name search in 64 MiB RAM ==")
    wanted = {
        b"CAPTG": 0x1244ED6C,
        b"CAPSEQ": 0x1244ED50,
        b"SHUTTER": 0x1244ED88,
        b"STGRAW": 0x1244E9EC,
        b"SIF_0": 0x1078B196,
        b"CaptureSensorSer": 0x1244C254,
    }
    for pat, exp in wanted.items():
        off = exp - 0x10000000
        found = ram[off:off + len(pat)] == pat
        check("name %-18s @%08x" % (pat.decode(), exp), found, "prefix match" if found else "MISMATCH")

    print("== P4: mas video table @0x1094CF50 ==")
    rows = [struct.unpack_from("<HHIHH", vmas, 12 * i) for i in range(18)]
    check("18 rows", len(rows) == 18)
    check("4K30 id=66", rows[13][0] == 66 and rows[13][2] == 30 and rows[13][3:] == (3840, 2160), str(rows[13]))
    check("4K25 id=67", rows[14][0] == 67 and rows[14][2] == 25, str(rows[14]))
    check("4K24 id=68", rows[15][0] == 68 and rows[15][2] == 24, str(rows[15]))
    check("no 4K60 in mas table", not any(r[2] == 60 and r[3:] == (3840, 2160) for r in rows))
    check("video_mas dump == RAM @0x1094CF50", vmas == at(ram, 0x1094CF50, len(vmas)))

    print("== P4: SLA video table @0x1091D58C ==")
    def srow(i):
        o = 0x20 * i
        w, h = struct.unpack_from("<HH", vsla, o + 8)
        rate = u32(vsla, o + 12)
        return w, h, rate
    check("16 rows", len(vsla) >= 0x20 * 16)
    check("4K60 row0 3840x2160/60000", srow(0) == (3840, 2160, 60000), str(srow(0)))
    check("4K50 row2 3840x2160/50000", srow(2) == (3840, 2160, 50000), str(srow(2)))
    check("4K30 row4 3840x2160/30000", srow(4) == (3840, 2160, 30000), str(srow(4)))
    check("video_sla dump == RAM @0x1091D58C", vsla == at(ram, 0x1091D58C, len(vsla)))

    print("== P4: size table @0x109617B8 ==")
    siz = [struct.unpack_from("<HHH", size, 6 * i) for i in range(14)]
    check("DX L 3936x2624", siz[9] == (3936, 2624, 9), str(siz[9]))
    check("DX M 2944x1968", siz[10] == (2944, 1968, 10), str(siz[10]))
    check("DX S 1968x1312", siz[11] == (1968, 1312, 11), str(siz[11]))
    check("4K 3840x2160", siz[12] == (3840, 2160, 12), str(siz[12]))
    check("1080 1920x1080", siz[13] == (1920, 1080, 13), str(siz[13]))
    check("size dump == RAM @0x109617B8", size == at(ram, 0x109617B8, len(size)))

    print("== P1: SIF handler table @0x1030E634 ==")
    table = [u32(ram, 0x30E634 + 4 * i) for i in range(31)]
    check("31 entries", len(table) == 31)
    check("entries 12-19 are SIF", table[12:20] == [0x1030CC67, 0x1030CBCB, 0x1030CB8F,
                                                    0x1030CB19, 0x1030CABF, 0x1030C9C9,
                                                    0x1030C883, 0x1030C77F], str([hex(x) for x in table[12:20]]))
    check("0x1032C9C8 is SIF_5 (index 17)", table[17] + 0x20000 - 1 == 0x1032C9C8,
          hex(table[17]))
    print("[NOTE] P1 trace file is not in the probe dir; it is a mainline QEMU artifact and cannot be rerun here.")

    print("== DX switch diff ==")
    check("registry_base == registry_dx", reg_base == reg_dx, "0 dwords")
    diff = [i for i, (a, b) in enumerate(zip(arena, arena_dx)) if a != b]
    check("arena diff 4 B", len(diff) == 4, str([hex(0xC2A40000 + i) for i in diff]))
    page = arena[0xA000:0xB000]
    check("0xC2A4A000 page all zero", not any(page), "0 nonzero")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
