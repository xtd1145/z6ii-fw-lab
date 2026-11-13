#!/usr/bin/env python3
"""120fps 静态管线可行性判定器 + 补丁工作单生成器（只读，绝不写固件）。

    python pipeline_120fps.py                 # 输出判定与工作单 pipeline_120fps.json
    python pipeline_120fps.py --dump-still DIR  # 拿到运行期模式记录转储后重判定

三条硬事实（本工具据此判定，不做乐观假设）：
  1. 读出模式表 mas+0x92cf50，18 条 ×12B `[u16 id][u16 0][u32 fps][u16 w][u16 h]`。
     最高像素率 = 249 MP/s（id 63 = 1920×1080@120，id 66 = 3840×2160@30）。
     现有机身的静态天花板 = FX 14fps × 24.5MP ≈ 343 MP/s。
  2. 该表在 mas 内没有任何 LE32 指针引用（只被基址+索引方式访问）→ 改行内容不会牵动别处，
     但也无法从静态镜像反推“静态拍摄路径按 id 选读出模式”这条间接关系是否成立。
  3. 6 个拍摄模式（Still / StillSilent / LvStill / LvStillSilent / LvMovieRec / LvMovie）
     的记录块 VMA 0x119df50c 起、步长 0x480，在镜像里**全为零**，字段运行期才写入。
     → “静态记录里是否有 readout-mode-index 字段”静态不可判，需 2 段运行期转储。

判定结论：
  路线 A（纯接线，等长改表即可）：成立 ⟺ 运行期 Still 记录与 LvMovieRec 记录在
    同一偏移上存在一个取值等于读出模式 id（63/66 量级）的字段。拿到转储即可确认或否证。
  路线 B（新增代码）：若 A 否证，120fps 静态 JPEG 必须新写“请求 120fps 读出 → JPEG 编码
    → 写卡”的管线代码，插入代码洞并重签名（依赖 K8 或门控字节非 2/4）。等长改表做不到。
  物理否决项（两条路线都救不了）：任何 MP/s > 343 的目标，如 DX 3936×2624@60(620)、@120(1240)、
    3840×2160@60(498)。
"""

import argparse
import hashlib
import json
import struct
import sys

MAS = "all_modules/Z6_2_0170/eg1850_mas_01700"
ORIG = r"C:\Users\BI\Downloads\Z6_2_0170.bin"
MODE_TABLE_OFF = 0x92CF50
MODE_TABLE_ROWS = 18
MODE_TABLE_STRIDE = 12
RECORD_BASE = 0x119DF50C
RECORD_STRIDE = 0x480
MODE_NAMES = ["Still", "StillSilent", "LvStill", "LvStillSilent", "LvMovieRec", "LvMovie"]
CEILING_MPS = 343.0          # FX 14fps @24.5MP，机内既有最大持续读出像素率
NEF_MB_AT_12BIT = {          # 12bit 无损单帧体积量级，用于写卡带宽核算
    "1920x1080": 1.0, "2080x1320": 1.5, "3936x2624": 8.0, "6048x4024": 19.0,
}


def read_rows(m):
    out = []
    for i in range(MODE_TABLE_ROWS):
        o = MODE_TABLE_OFF + i * MODE_TABLE_STRIDE
        mid, pad, fps, w, h = struct.unpack_from("<HHIHH", m, o)
        if pad != 0 or fps == 0 or w == 0 or h == 0:
            continue
        out.append({"off": o, "vma": o + 0x10000000, "id": mid, "fps": fps,
                    "w": w, "h": h, "mps": round(w * h * fps / 1e6, 1)})
    return out


def targets():
    """本轮要实现的目标，逐条做物理核算。"""
    return [
        {"name": "DX 60fps JPEG（原目标 13-A）", "w": 3936, "h": 2624, "fps": 60, "kind": "jpg"},
        {"name": "DX 30fps 不降位深 RAW（目标 13-B）", "w": 3936, "h": 2624, "fps": 30, "kind": "raw"},
        {"name": "2MP 合并读出 120fps JPEG（本次要求）", "w": 1920, "h": 1080, "fps": 120, "kind": "jpg"},
        {"name": "1.3x 合并读出 120fps JPEG", "w": 2080, "h": 1320, "fps": 120, "kind": "raw"},
        {"name": "4K60（代理曾建议照抄 Z8）", "w": 3840, "h": 2160, "fps": 60, "kind": "mov"},
    ]


def physics(t):
    mps = t["w"] * t["h"] * t["fps"] / 1e6
    frame_mb = NEF_MB_AT_12BIT.get("%dx%d" % (t["w"], t["h"]), 0.7 if t["kind"] == "jpg" else 8.0)
    card = frame_mb * t["fps"]
    ok = mps <= CEILING_MPS
    return {"mps": round(mps, 1), "card_mb_s": round(card, 1),
            "readout_ok": ok,
            "card_ok": card <= 250,
            "verdict": "读出带宽可行" if ok else "读出带宽超限：物理不可，任何补丁都救不了"}


def probe_static(m, rows):
    """Route A 的静态前提检查：模式记录块是否为空、表是否可安全等长改写。"""
    recs = {}
    for i, name in enumerate(MODE_NAMES):
        o = RECORD_BASE - 0x10000000 + i * RECORD_STRIDE
        blob = m[o:o + RECORD_STRIDE]
        nz = [(j, b) for j, b in enumerate(blob) if b]
        recs[name] = {"vma": RECORD_BASE + i * RECORD_STRIDE, "file_off": o,
                      "nonzero_bytes": len(nz),
                      "first_nonzero": [("0x%x" % j, "0x%02x" % b) for j, b in nz[:6]]}
    ids = sorted({r["id"] for r in rows})
    return {"mode_records": recs, "readout_mode_ids": ids,
            "table_has_pointer_refs": False,
            "note": "记录块非零字节数 < 32 视为运行期填充；此时 Route A 静态不可判"}


def dump_targets():
    """拿到这两段转储就能把 Route A 判死或判活。"""
    return [{"label": "静态 Still 模式记录", "addr": RECORD_BASE, "len": RECORD_STRIDE},
            {"label": "LvMovieRec 模式记录（120fps 视频在用）",
             "addr": RECORD_BASE + 4 * RECORD_STRIDE, "len": RECORD_STRIDE},
            {"label": "门控字节", "addr": 0xC1887BC0, "len": 0x10},
            {"label": "校验上下文（含 K8）", "addr": 0xC2B2B608, "len": 0x34}]


def compare_dump(still_dir, recs):
    """有运行期转储后：找两记录间同偏移的单值字段，且其值落在读出模式 id 集合内。"""
    import os
    ids = set(recs["readout_mode_ids"])
    pairs = []
    for fn in sorted(os.listdir(still_dir)):
        if "still" in fn.lower() and "lv" not in fn.lower():
            still = open(os.path.join(still_dir, fn), "rb").read()
        if "movierec" in fn.lower():
            movie = open(os.path.join(still_dir, fn), "rb").read()
    if len(still) < RECORD_STRIDE or len(movie) < RECORD_STRIDE:
        return {"error": "转储长度不足 0x480"}
    for o in range(0, RECORD_STRIDE - 4):
        a, b = struct.unpack_from("<I", still, o), struct.unpack_from("<I", movie, o)
        if a != b and (a in ids or b in ids):
            pairs.append({"field_off": "0x%x" % o, "still": a, "movierec": b})
    return {"candidate_readout_fields": pairs,
            "route_a_live": bool(pairs),
            "route": "A 等长改表：把 Still 的该字段写成 63" if pairs
                     else "B 需新增管线代码：无 readout-index 间接层"}


def main():
    ap = argparse.ArgumentParser(description="120fps 管线可行性判定（只读）")
    ap.add_argument("--mas", default=MAS)
    ap.add_argument("--dump-still", help="放入运行期两段模式记录转储的目录，用于终审")
    ap.add_argument("--out", default="pipeline_120fps.json")
    args = ap.parse_args()

    m = open(args.mas, "rb").read()
    raw = open(ORIG, "rb").read()
    assert len(raw) == 49002888, "原始固件长度变了，停止"
    rows = read_rows(m)

    print("读出模式表 mas+0x%x（%d 条，像素率上限 %.0f MP/s 对照机内天花板 %.0f MP/s）"
          % (MODE_TABLE_OFF, len(rows), max(r["mps"] for r in rows), CEILING_MPS))
    for r in sorted(rows, key=lambda x: -x["mps"]):
        print("  id=%-4d %4dx%4d @%-4d = %6.1f MP/s" % (r["id"], r["w"], r["h"], r["fps"], r["mps"]))

    print("\n目标逐条物理核算：")
    phys = []
    for t in targets():
        p = physics(t)
        phys.append({**t, **p})
        print("  %-34s %6.1f MP/s  写卡 %6.1f MB/s  → %s"
              % (t["name"], p["mps"], p["card_mb_s"], p["verdict"]))

    st = probe_static(m, rows)
    print("\n静态前提检查：")
    for name, r in st["mode_records"].items():
        print("  %-14s VMA=0x%08x  非零字节 %d" % (name, r["vma"], r["nonzero_bytes"]))
    print("  模式记录几乎全零 → “静态路径按 id 选读出模式”的间接层是否存在，静态不可判")

    route = {"route_A": "undecidable_static", "route_B": "needed_if_A_fails",
             "ceiling_mps": CEILING_MPS}
    if args.dump_still:
        cmp = compare_dump(args.dump_still, st)
        route["dump_verdict"] = cmp
        print("\n运行期转储终审：%s" % json.dumps(cmp, ensure_ascii=False)[:600])

    spec = {"generated_from_mas_sha256": hashlib.sha256(m).hexdigest(),
            "original_firmware_sha256": hashlib.sha256(raw).hexdigest(),
            "readout_table": rows, "physics": phys, "static_probe": st, "routes": route,
            "capture_workorder": dump_targets(),
            "write_order": [],
            "why_no_write_order_yet": "等长改表的字节只有在全新代码里才有意义；"
                                      "Route A 未确认前，任何字节建议都是猜测，故本工具拒绝输出伪补丁"}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    print("\n工作单 → %s\n下一步只有一件事：按 capture_workorder 取两段 0x480 记录转储"
          "（k8_capture.py --text/--dump 收），Route A 立刻判活或判死。" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
