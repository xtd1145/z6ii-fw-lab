"""NP3 载体生成器 —— 把 ZLOG8 曲线打包成 Z6 II 可导入的自定义 Picture Control。

容器模型按**真实样本实测**（2026-10-30 更正，推翻此前的 magic 假设）：
  真实样本  work/color/tmpout/card/PICCON219.NP3  (1234 B, version 0210, name 'A-X rossandhisjpegs')
  参考解析器 work/color/z6ii_np3.py                (color 线，只读，本文件不修改它)

实测结构：
  偏移 0    "NCP\\0" 标签
  随后      TLV 流 {BE32 tag, BE32 len, body}
              0x0100 版本 ASCII 4B（版本门：前缀须 01/02，见 0x103ba814）
              0x0200 名称 20B
              0x0300..0x1600 标量 (value, step) 各 2B  —— 固件 TBB 表只覆盖到这里
              >0x1600 被"读长度并跳过"（0x103ba7b2）
              0x10100 说明文本 248B
              0x0002 tone curve 0x242 —— 放在最后
  结尾      BE32 = 0

**0x043A0080 不是 NP3 魔数**，而是文件打开成功的返回码：`0x104B6710` 处
`ldr r5,[pc,#0x264]` 取到 `0x043A0089`，`subs r5,#9` 后成功路径返回 `0x043A0080`；
`bl 0x104B67A8` 之后 `0x103BA5F4` / `0x103BB70C` 两处 `cmp r0,#0x043A0080` 检查的是打开状态。
真实文件头只有 `NCP\\0`，样本里搜不到该常量是正常的。

**两种布局**（`--layout`）：
  native   v1.70 序列化器 `0x103BB862` 导出的紧凑布局，总长 0x342=834，tag 2 @+0xF4，表 @+0x13C
  extended PICCON219.NP3 的 1234 B 布局，多出 0x1700..0x1e00 标量、0x1f00、0x2000、0x10100
  读取器按 TLV 扫描找 tag 2，两种都能载入；**产物默认按原生布局生成**。

曲线块（0x242 = 578 = 0x40 + 257×2，精确成立）：
  payload[0x00:0x08] 头  '49 30 00 ff 0a e4 01 00'
  payload[0x08:0x40] 锚点/元数据摘要
  payload[0x40:]     **257 个 BE16**，满量程 **0x7FFF**（15-bit），样本实测 1285..29298 单调

装载位置（模拟执行 0x103bb7b4 实测）：
  B:\\NIKON\\CUSTOMPC\\PICCON01.NP3 … PICCON99.NP3；槽位 0xC8..0x12A，index=slot-0xC7
  盘符 A/B 由 0x103bc77e 改写**路径字符串首字符**，与文件内容无关
"""
import argparse
import json
import os
import re
import stat
import struct
import sys

import numpy as np

LABEL = b"NCP\x00"
TAIL = b"\x00\x00\x00\x00"
VERSION_TAG = 0x0100
NAME_TAG = 0x0200
DESC_TAG = 0x10100
CURVE_TAG = 0x0002
STORAGE_MAX = 0x1600            # 固件 TBB 表上界，更大的 tag 被跳过
CURVE_LEN = 0x242               # 578
CURVE_HDR = 0x40                # 块头 + 元数据区
CURVE_POINTS = 257              # 实测：不是 256
CURVE_FULL = 0x7FFF             # 实测：不是 65535
DESC_LEN = 248

def _repo_root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "work")) and os.path.isdir(os.path.join(d, "all_modules")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.getcwd()


DEFAULT_SAMPLE = os.path.join(_repo_root(), "work", "color", "tmpout", "card", "PICCON219.NP3")
DEFAULT_VERSION = "0210"        # 版本门前缀必须是 01/02，03xx 被拒
ZLOG8_NAME = "ZLOG8"
ZLOG8_DESC = ("ZLOG8 - 8-bit in-camera log curve. "
              "Toe X<=0.05 power 2.1030, main ln segment 43.73 code/stop. "
              "18% gray -> code 127. BT.709. Not Nikon N-Log.")

CONSTRAINTS = {
    0x0400: ("value", lambda v: v < 3, "value < 3"),
    0x0500: ("step", lambda s: s == 1, "step == 1"),
    0x0600: ("step", lambda s: s == 4, "step == 4"),
}

# ---- 写目标安全校验：绝不允许落到裸设备或磁盘镜像的偏移 0 ----
RAW_IMG_EXT = (".img", ".iso", ".bin", ".bi", ".dec", ".dmg", ".vhd", ".vhdx",
               ".vmdk", ".raw", ".qd", ".r1", ".dd")
DEV_PREFIX = re.compile(r"^(\\\\\.\\|//\./|/dev/|/proc/|/sys/)", re.I)


def assert_safe_target(path, kind="输出文件", require_parent=True, must_exist=False):
    if path is None:
        return None
    p = os.path.abspath(path)
    if DEV_PREFIX.match(path) or DEV_PREFIX.match(p):
        raise SystemExit(f"拒绝写入设备命名空间：{path}（{kind}）——本工具只写宿主普通文件")
    if p.lower().endswith(RAW_IMG_EXT):
        raise SystemExit(
            f"拒绝把 {kind} 指向磁盘/卡镜像文件：{p}\n"
            f"  按文件方式写这种路径会从偏移 0 覆盖，直接毁掉 MBR/引导扇区。\n"
            f"  请先挂载卡镜像（或用 QEMU vvfat 指向目录），再把目录交给 --card-dir。")
    if os.path.exists(p):
        st = os.stat(p)
        if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
            raise SystemExit(f"拒绝写入块/字符设备：{p}（{kind}）")
    elif must_exist:
        raise SystemExit(f"{kind} 不存在：{p}")
    parent = os.path.dirname(p)
    if require_parent and not os.path.isdir(parent):
        raise SystemExit(f"{kind} 的父目录不是已存在目录：{parent}")
    rel = os.path.normpath(p).lower()
    for banned in ("all_modules", "fw_set", "fw_single", "fw_verified", "ref_fw", "probes"):
        if os.sep + banned + os.sep in os.sep + rel + os.sep:
            raise SystemExit(f"拒绝写入只读参考区：{p}（{kind}）")
    return p


def assert_card_dir(card_dir):
    d = os.path.abspath(card_dir)
    if not os.path.isdir(d):
        raise SystemExit(
            f"--card-dir 必须是**已存在的目录挂载点**，当前 {card_dir} 不是目录。\n"
            f"  若手上是卡镜像：先挂载（Windows OsFMount/Dokan；Linux mount -o loop；"
            f"QEMU 用 vvfat 直接指向目录），再把挂载点传进来。\n"
            f"  本工具绝不按扇区偏移写镜像。")
    return d


# ---------------- 解析 ----------------
def parse(data, name_hint=""):
    """按真实结构解析，并模拟固件 loader 的接受/跳过/拒绝判定。"""
    rep = {"label_ok": False, "version": None, "name": None, "chunks": [],
           "curve": None, "curve_head": None, "tail_ok": False, "checks": [],
           "loader": [], "size": len(data)}

    def chk(n, ok, conf, detail=""):
        rep["checks"].append({"name": n, "ok": bool(ok), "confidence": conf, "detail": detail})

    rep["label_ok"] = data[:4] == LABEL
    chk('偏移0 "NCP\\0" 标签', rep["label_ok"], "高（样本实测）", f"got {data[:4]!r}")
    if not rep["label_ok"]:
        return rep

    off = 4
    while off + 8 <= len(data):
        tag, ln = struct.unpack_from(">II", data, off)
        if tag == 0 and ln == 0:
            break
        if off + 8 + ln > len(data):
            chk(f"tag 0x{tag:05x} 长度", False, "高", f"len {ln} 越界")
            return rep
        body = data[off + 8: off + 8 + ln]
        rep["chunks"].append({"tag": tag, "len": ln, "off": off, "body": body})
        if tag == VERSION_TAG:
            rep["version"] = body.decode("latin-1").strip("\x00 ")
            chk("版本门 前缀∈{01,02}", rep["version"][:2] in ("01", "02"),
                "高（0x103ba814）", rep["version"])
        elif tag == NAME_TAG:
            rep["name"] = body.split(b"\x00", 1)[0].decode("latin-1")
            chk("名称字段 len==20", ln == 20, "高（样本实测）", repr(rep["name"]))
        elif tag == CURVE_TAG:
            chk("曲线块 len==0x242", ln == CURVE_LEN, "高（0x103bb6da）", f"0x{ln:x}")
            rep["curve_head"] = body[:CURVE_HDR]
            if ln >= CURVE_HDR + CURVE_POINTS * 2:
                c = np.frombuffer(body, dtype=">u2", count=CURVE_POINTS, offset=CURVE_HDR)
                rep["curve"] = c.astype(np.int64)
                chk("曲线 257 点单调非降", bool(np.all(np.diff(c) >= 0)), "高",
                    f"min={c[0]} max={c[-1]}")
                chk("曲线值域 <=0x7FFF", int(c.max()) <= CURVE_FULL, "高（样本实测）",
                    f"max=0x{c.max():04X}")
        off += 8 + ln
    rep["tail_ok"] = data[-4:] == TAIL
    chk("尾部 BE32 == 0", rep["tail_ok"], "高（样本实测）", data[-4:].hex(" "))
    rep["loader"] = simulate_loader([(x["tag"], x["body"]) for x in rep["chunks"]])
    return rep


def simulate_loader(chunks):
    """按固件规则判断每个 tag 会被接受 / 跳过 / 拒绝。"""
    lines = []
    for tag, body in chunks:
        if tag == CURVE_TAG:
            lines.append(("接受(tone curve)" if len(body) == CURVE_LEN else "丢弃(块长不符)", tag,
                          f"{CURVE_POINTS} 点 @+0x{CURVE_HDR:X}，值域 0..0x{CURVE_FULL:X}"))
            continue
        if tag == VERSION_TAG:
            v = body.decode("latin-1").strip("\x00 ")
            lines.append(("接受(版本)" if v[:2] in ("01", "02") else "拒绝(版本门)", tag, v))
            continue
        if tag == NAME_TAG:
            lines.append(("接受(名称)", tag, body.split(b"\x00", 1)[0].decode("latin-1")))
            continue
        if tag > STORAGE_MAX:
            lines.append(("跳过(>0x1600)", tag, f"len={len(body)}；Z6II 不消费此 tag"))
            continue
        if len(body) == 2:
            v, s = body[0], body[1]
            if tag in CONSTRAINTS:
                which, ok, desc = CONSTRAINTS[tag]
                got = v if which == "value" else s
                if not ok(got):
                    lines.append(("拒绝", tag, f"{which}={got} 违反 {desc}"))
                    continue
            lines.append(("接受(标量)", tag, f"value=0x{v:02X} step={s}"))
        else:
            lines.append(("跳过(长度非2)", tag, f"len={len(body)}"))
    return lines


# ---------------- 曲线块 ----------------
def curve_block(be16, head_tmpl=None, anchors=True):
    """构造 tag 0x2 的 578 字节 payload。
    head_tmpl：真实样本的 64 字节块头（默认复用它，锚点区语义未完全确认，保守照抄）。
    anchors=True 且无模板时，按 color 线算法生成 头+锚点摘要。"""
    assert len(be16) == CURVE_POINTS, f"曲线必须 {CURVE_POINTS} 点，实际 {len(be16)}"
    table = np.asarray(be16, dtype=">u2").tobytes()      # 不得再 astype 原生 uint16，那会变回小端
    if head_tmpl is not None:
        head = bytes(head_tmpl[:CURVE_HDR])
    elif anchors:
        head = bytearray(CURVE_HDR)
        head[0:2] = b"\x49\x30"
        head[2:4] = b"\x00\xff"
        head[4:6] = b"\x00\xff"
        head[6:8] = b"\x01\x00"
        y = np.asarray(be16, float) / CURVE_FULL
        pts = []
        for x in (0, 128, 255):
            v = y[round(x * (CURVE_POINTS - 1) / 255)]
            pts += [x, max(0, min(255, round(v * 255)))]
        meta = bytearray([len(pts) // 2]) + bytearray(pts)
        head[8:8 + len(meta)] = meta[:CURVE_HDR - 8]
        head = bytes(head)
    else:
        head = bytes(CURVE_HDR)
    block = head + table
    assert len(block) == CURVE_LEN, (len(block), CURVE_LEN)
    return block


# ---------------- 组装 ----------------
def build(curve_be16, name=ZLOG8_NAME, version=DEFAULT_VERSION, desc=ZLOG8_DESC,
          template_chunks=None, head_tmpl=None, replace_desc=True):
    """按真实 tag 顺序组装 NP3。template_chunks 来自真实样本 -> 标量区原样继承。"""
    tmpl = list(template_chunks or [])
    has = {t for t, _ in tmpl}
    out = []
    out.append((VERSION_TAG, version.encode("latin-1")[:4].ljust(4, b"\x00")))
    out.append((NAME_TAG, name.encode("latin-1")[:19].ljust(20, b"\x00")))
    for tag, body in tmpl:
        if tag in (VERSION_TAG, NAME_TAG, CURVE_TAG):
            continue
        if tag == DESC_TAG and replace_desc:
            continue                      # 样本里是第三方版权文本，必须替换
        out.append((tag, body))
    if replace_desc or DESC_TAG not in has:
        db = desc.encode("latin-1")[:DESC_LEN].ljust(DESC_LEN, b"\x00")
        out.append((DESC_TAG, db))
    blob = bytearray(LABEL)
    seen, ordered = set(), []
    for tag, body in out:
        if tag in seen:
            continue
        seen.add(tag)
        ordered.append((tag, body))
    ordered.append((CURVE_TAG, curve_block(curve_be16, head_tmpl)))   # 曲线块永远排在最后
    for tag, body in ordered:
        blob += struct.pack(">II", tag, len(body)) + body
    blob += TAIL
    return bytes(blob), ordered


def load_curve(path):
    """接受 zlog8_curve_257.txt（列或带表头）或 pipeline 直接计算。"""
    if path is None:
        import pipeline as P
        return P.curve_be16_257()
    rows = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        try:
            rows.append(int(float(tok[-1])))
        except ValueError:
            continue
    v = np.asarray(rows, dtype=np.int64)
    if v.size == 256:          # 8-bit 码值表 -> 重采样成 257 个 15-bit
        c8 = v.astype(float) / 255.0
        u = np.linspace(0, 1, CURVE_POINTS)
        v = np.rint(np.interp(u, np.linspace(0, 1, 256), c8) * CURVE_FULL).astype(np.int64)
    if v.size != CURVE_POINTS:
        raise SystemExit(f"曲线需 {CURVE_POINTS} 点（或 256 点 8-bit 表），实际 {v.size}")
    return v


def calibrate(sample_path, out_profile=None):
    """从真实样本反推 profile：点数、值域、块头模板、标量区、tag 顺序。"""
    data = open(sample_path, "rb").read()
    rep = parse(data)
    if not rep["label_ok"]:
        raise SystemExit(f"样本缺少 NCP\\0 标签：{sample_path}")
    curve = rep["curve"]
    prof = {
        "source_sample": sample_path,
        "sample_size": rep["size"],
        "label": LABEL.decode("latin-1"),
        "magic_is_open_code": ("0x043A0080 是文件打开成功返回码"
                               "（0x104B6710: ldr r5=0x043A0089; subs r5,#9），不是文件魔数"),
        "curve_points": int(curve.size),
        "curve_off_in_block": CURVE_HDR,
        "value_domain": CURVE_FULL if curve.max() <= CURVE_FULL else int(curve.max()),
        "observed_curve_min": int(curve.min()),
        "observed_curve_max": int(curve.max()),
        "curve_head_hex": bytes(rep["curve_head"]).hex(" "),
        "version": rep["version"],
        "tag_order": [f"0x{c['tag']:05x}" for c in rep["chunks"]],
        "scalars": [{"tag": f"0x{c['tag']:05x}", "body": c["body"].hex(" ")}
                    for c in rep["chunks"] if len(c["body"]) == 2],
        "tail": TAIL.hex(" "),
    }
    if out_profile:
        out_profile = assert_safe_target(out_profile, "--profile（定标输出）")
        json.dump(prof, open(out_profile, "w"), indent=2, ensure_ascii=False)
    return prof, rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve", help="257 点曲线文本；缺省由 pipeline 计算")
    ap.add_argument("--out", help="写出 NP3")
    ap.add_argument("--profile", help="profile.json 路径")
    ap.add_argument("--calibrate", metavar="SAMPLE.NP3", help="从真实样本定标 profile")
    ap.add_argument("--inspect", metavar="FILE", help="解析任意 NP3（样本或自产物）并模拟 loader")
    ap.add_argument("--from-sample", default=DEFAULT_SAMPLE, help="结构模板：真实 NP3 样本（只读）")
    ap.add_argument("--name", default=ZLOG8_NAME)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--no-desc-replace", action="store_true",
                    help="保留模板里的 0x10100 文本（默认替换，避免带入第三方版权）")
    ap.add_argument("--layout", choices=("native", "extended"), default="native",
                    help="native=v1.70 紧凑 0x342(834B)，tag2@+0xF4、表@+0x13C；"
                         "extended=沿用样本 1234B 布局")
    ap.add_argument("--slot", type=lambda s: int(s, 0), default=0xC8)
    ap.add_argument("--card-dir", help="按相机目录落盘 <挂载点>/NIKON/CUSTOMPC/PICCONnn.NP3")
    a = ap.parse_args()

    if a.calibrate:
        prof, rep = calibrate(a.calibrate, a.profile)
        print(f"[定标] {a.calibrate}  {rep['size']} B  -> {a.profile or '(未写文件)'}")
        print(f"  curve_points = {prof['curve_points']}   value_domain = 0x{prof['value_domain']:X}"
              f"   样本曲线 {prof['observed_curve_min']}..{prof['observed_curve_max']}")
        print(f"  version = {prof['version']}  name = {rep['name']!r}")
        print(f"  tag 顺序 = {' '.join(prof['tag_order'])}")
        print(f"  块头 = {prof['curve_head_hex']}")
        for c in rep["checks"]:
            print(f"  [{'OK ' if c['ok'] else 'BAD'}] {c['name']}（{c['confidence']}）{c['detail']}")
        return 0 if all(c["ok"] for c in rep["checks"]) else 1

    if a.inspect:
        data = open(a.inspect, "rb").read()
        if len(data) == 0x342:
            import z6ii_np3_real as REAL
            rep = REAL.audit(data)
            print(f"[解析·原生] {a.inspect}  {rep['size']} B  version={rep['version']} "
                  f"name={rep['name']!r}  tag2@0x{(rep['curve_tag_off'] or 0):X} "
                  f"表@0x{(rep['table_off'] or 0):X}")
            for c in rep["checks"]:
                print(f"  [{'OK ' if c['ok'] else 'BAD'}] {c['name']}（{c['confidence']}）{c['detail']}")
            if rep["curve"] is not None and rep.get("rev16_mem") is not None:
                cu, m = rep["curve"], rep["rev16_mem"]
                print(f"    curve 文件 {int(cu[0])}..{int(cu[-1])}；rev16 后内存 "
                      f"{int(m[0])}..{int(m[255])}，第 257 字 0x{int(cu[256]):04X}->0x{int(m[256]):04X}")
            return 0 if all(x["ok"] for x in rep["checks"]) else 1
        rep = parse(data)
        print(f"[解析·通用] {a.inspect}  {rep['size']} B  version={rep['version']} name={rep['name']!r}")
        for c in rep["checks"]:
            print(f"  [{'OK ' if c['ok'] else 'BAD'}] {c['name']}（{c['confidence']}）{c['detail']}")
        for st, tag, det in rep["loader"]:
            print(f"    tag=0x{tag:05x}  {st}  {det}")
        if rep["curve"] is not None:
            c = rep["curve"]
            print(f"    curve head={list(map(int, c[:4]))} tail={list(map(int, c[-3:]))}")
        return 0 if all(x["ok"] for x in rep["checks"]) else 1

    curve = load_curve(a.curve)
    sample_bytes = None
    if a.from_sample and os.path.exists(a.from_sample):
        sample_bytes = open(a.from_sample, "rb").read()

    if a.layout == "native":
        import z6ii_np3_real as REAL
        sc, hm = REAL.scalars_from_extended(sample_bytes) if sample_bytes else ({}, None)
        blob = REAL.build(curve, name=a.name, version=a.version, scalars=sc, head_meta=hm)
        rep = REAL.audit(blob)
        ordered = rep["chunks"]
        print(f"[布局] v1.70 原生紧凑 0x{REAL.TOTAL:X}  "
              f"{'继承样本标量与块头' if sample_bytes else '无样本，标量取默认'}")
    else:
        head_tmpl, tmpl_chunks = None, []
        if sample_bytes:
            srep = parse(sample_bytes)
            if srep["curve"] is not None:
                head_tmpl = srep["curve_head"]
                tmpl_chunks = [(c["tag"], c["body"]) for c in srep["chunks"]
                               if c["tag"] != CURVE_TAG]
                print(f"[布局] 扩展 1234B  模板 {a.from_sample}  "
                      f"继承 {len(tmpl_chunks)} 个 tag + 64B 块头")
        else:
            print("[布局] 扩展 1234B  无样本可用，块头按算法生成，置信度降低")
        blob, ordered = build(curve, name=a.name, version=a.version,
                              template_chunks=tmpl_chunks, head_tmpl=head_tmpl,
                              replace_desc=not a.no_desc_replace)
        rep = parse(blob)
    bad = [c["name"] for c in rep["checks"] if not c["ok"]]
    print(f"[生成] {len(blob)} B  tags={len(ordered)}  "
          f"曲线 {curve.min()}..{curve.max()} (0..0x{CURVE_FULL:X})  name={a.name!r}")
    for c in rep["checks"]:
        print(f"  [{'OK ' if c['ok'] else 'BAD'}] {c['name']}（{c['confidence']}）{c['detail']}")
    if a.out:
        a.out = assert_safe_target(a.out, "--out")
        open(a.out, "wb").write(blob)
        print(f"  -> {a.out}")
    if a.card_dir:
        if not (0xC8 <= a.slot <= 0x12A):
            raise SystemExit(f"槽位 {hex(a.slot)} 不在内置命名区 0xC8..0x12A")
        fn = assert_safe_target(os.path.join(assert_card_dir(a.card_dir), "NIKON", "CUSTOMPC",
                                             f"PICCON{a.slot - 0xC7:02d}.NP3"),
                                "--card-dir", require_parent=False)
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        open(fn, "wb").write(blob)
        print(f"  -> {fn}   （盘符由相机运行期决定，文件内容无需含 A/B）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
