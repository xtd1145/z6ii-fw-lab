"""Nikon Z6II v1.70 **原生** NP3 布局（0x342 = 834 B）的读写与模拟 loader。

与 PICCON219.NP3（1234 B 扩展布局）的区别：v1.70 自己导出到卡时只写
tag 0x0100/0x0200/0x0300..0x1600 + tone curve，**不写** 0x1700 以上、0x1f00、0x2000、0x10100。
读取器按 TLV 扫描找 tag 2，所以扩展布局也能载入，但产物应优先按原生长度生成。

证据链（eg1850_mas_01700，VMA = 0x10000000 + 文件偏移）：
  0x103bbdd4  保存到卡入口，内含 `B:\\NIKON\\CUSTOMPC\\` 与 `%s%s%02d.%s`
  0x103bb862  序列化器：`movw r8,#0x242`，依次写 tag 0x100..0x1600
  0x103bc792  rev16 循环：`movw r4,#0x100` + `cmp r1,r4` -> **只转前 256 个字**，
              第 257 个字不转换，故它在内存里仍按文件的大端原样存在
  0x104b6710  `ldr r5,[pc,#0x264]` 取 0x043A0089，`subs r5,#9` -> 成功路径返回 **0x043A0080**
  0x103ba5f4 / 0x103bb70c  两处 `cmp r0, #0x043A0080` 检查的是**文件打开状态**，不是文件魔数
              真实文件头只有 `NCP\\0`

原生布局（总长 0x342）：
  +0x000  "NCP\\0"
  +0x004  {BE32 tag=0x0100, BE32 len=4}   ASCII 版本 "0210"（版本门要求前缀 01/02）
  +0x010  {tag=0x0200, len=20}            名称
  +0x02C  {tag=0x0300..0x1600, len=2}×20  (value, step)；(0x1600-0x300)/0x100+1 = 20
  +0x0F4  {tag=0x0002, len=0x242}         4+12+28+20*10 = 244 = 0xF4
  +0x0FC  body 0x242：+0x00..0x3F 块头/元数据，+0x40 起 257 个 BE16 -> 表在文件 +0x13C
  +0x33E  BE32 零尾                        244+8+578 = 830 = 0x33E；总长 834 = 0x342
"""
import struct

import numpy as np

LABEL = b"NCP\x00"
TOTAL = 0x342                  # 834
CURVE_TAG_OFF = 0x0F4
CURVE_BODY_OFF = 0x0FC
TABLE_OFF = 0x13C              # = 0xFC + 0x40
TAIL_OFF = 0x33E
CURVE_TAG = 0x0002
CURVE_LEN = 0x242
CURVE_HDR = 0x40
CURVE_POINTS = 257
REV16_WORDS = 0x100            # 固件只转前 256 个字
CURVE_FULL = 0x7FFF
SCALAR_TAGS = [0x300 + 0x100 * i for i in range(20)]      # 0x300..0x1600
VERSION_TAG, NAME_TAG = 0x0100, 0x0200
CONSTRAINTS = {0x0400: ("value", lambda v: v < 3, "value<3"),
               0x0500: ("step", lambda s: s == 1, "step==1"),
               0x0600: ("step", lambda s: s == 4, "step==4")}


def tlvs(tag, body):
    return struct.pack(">II", tag, len(body)) + body


def build(curve_be16, name="ZLOG8", version="0210", scalars=None, head_meta=None):
    """按 v1.70 原生布局组装。scalars: {tag: (value, step)}，head_meta: 64 字节块头模板。"""
    c = np.asarray(curve_be16, dtype=np.int64)
    if c.size != CURVE_POINTS:
        raise ValueError(f"曲线需 {CURVE_POINTS} 点，实际 {c.size}")
    if version[:2] not in ("01", "02"):
        raise ValueError(f"版本门拒绝 {version}（前缀须 01/02）")
    sc = dict(scalars or {})
    body = bytearray(CURVE_HDR)
    if head_meta is not None:
        body[:CURVE_HDR] = bytes(head_meta[:CURVE_HDR])
    blob = bytearray(LABEL)
    blob += tlvs(VERSION_TAG, version.encode("latin-1")[:4].ljust(4, b"\x00"))
    blob += tlvs(NAME_TAG, name.encode("latin-1")[:19].ljust(20, b"\x00"))
    for tag in SCALAR_TAGS:
        v, s = sc.get(tag, (0x80, 0))
        if tag in CONSTRAINTS:
            which, ok, desc = CONSTRAINTS[tag]
            got = v if which == "value" else s
            if not ok(got):
                raise ValueError(f"tag 0x{tag:03x} {which}={got} 违反 {desc}")
        blob += tlvs(tag, struct.pack("BB", v & 0xFF, s & 0xFF))
    if len(blob) != CURVE_TAG_OFF:
        raise ValueError(f"tag 0x2 应落在 +0x{CURVE_TAG_OFF:X}，实际 +0x{len(blob):X}")
    table = np.clip(c, 0, CURVE_FULL).astype(">u2").tobytes()
    block = bytes(body) + table
    if len(block) != CURVE_LEN:
        raise ValueError(f"曲线块应为 0x{CURVE_LEN:X}，实际 {len(block)}")
    blob += tlvs(CURVE_TAG, block)
    if len(blob) != TAIL_OFF:
        raise ValueError(f"零尾应落在 +0x{TAIL_OFF:X}，实际 +0x{len(blob):X}")
    blob += b"\x00\x00\x00\x00"
    if len(blob) != TOTAL:
        raise ValueError(f"总长应为 0x{TOTAL:X}={TOTAL}，实际 {len(blob)}")
    return bytes(blob)


def parse(data):
    """解析原生或扩展 NP3；返回结构、257 点文件值、以及模拟 rev16 后的内存视图。"""
    out = {"size": len(data), "label_ok": data[:4] == LABEL, "version": None, "name": None,
           "chunks": [], "curve": None, "curve_tag_off": None, "table_off": None,
           "head_meta": None, "tail_ok": data[-4:] == b"\x00\x00\x00\x00", "native": len(data) == TOTAL}
    if not out["label_ok"]:
        return out
    off = 4
    while off + 8 <= len(data):
        tag, ln = struct.unpack_from(">II", data, off)
        if tag == 0 and ln == 0:
            break
        body = data[off + 8: off + 8 + ln]
        if len(body) != ln:
            out["truncated_at"] = tag
            return out
        out["chunks"].append((tag, body, off))
        if tag == VERSION_TAG:
            out["version"] = body.decode("latin-1").strip("\x00 ")
        elif tag == NAME_TAG:
            out["name"] = body.split(b"\x00", 1)[0].decode("latin-1")
        elif tag == CURVE_TAG:
            out["curve_tag_off"] = off
            if ln >= CURVE_HDR + CURVE_POINTS * 2:
                out["head_meta"] = body[:CURVE_HDR]
                out["table_off"] = off + 8 + CURVE_HDR
                out["curve"] = np.frombuffer(body, dtype=">u2",
                                             count=CURVE_POINTS, offset=CURVE_HDR).astype(np.int64)
        off += 8 + ln
    return out


def simulate_rev16(curve):
    """模拟 0x103bc792：前 REV16_WORDS 个字做 rev16（大端文件 -> 小端内存），第 257 个字不动。
    返回相机内存里读到的 257 个整数值。"""
    c = np.asarray(curve, dtype=np.uint16)
    mem = np.zeros(c.size, dtype=np.uint16)
    n = min(REV16_WORDS, c.size)
    mem[:n] = c[:n]                                        # rev16 后数值等于文件大端解释值
    if c.size > n:
        # 未转换：文件字节 [hi,lo] 原样留在内存，小端 CPU 读出即字节交换后的值
        tail = c[n:].astype(np.uint32)
        mem[n:] = (((tail & 0xFF) << 8) | ((tail >> 8) & 0xFF)).astype(np.uint16)
    return mem.astype(np.int64)


def scalars_from_extended(sample_blob):
    """从扩展布局样本里取 0x0300..0x1600 的标量，作为原生布局的中性/真实取值。"""
    rep = parse(sample_blob)
    sc = {}
    for tag, body, _ in rep["chunks"]:
        if tag in SCALAR_TAGS and len(body) == 2:
            sc[tag] = (body[0], body[1])
    return sc, rep["head_meta"]


def byteswap(v):
    v = int(v) & 0xFFFF
    return ((v & 0xFF) << 8) | (v >> 8)


def audit(data):
    """按 v1.70 原生布局的硬约束逐项审计，返回与 np3_gen.parse 兼容的报告结构。"""
    r = parse(data)
    checks = []

    def chk(n, ok, conf, det=""):
        checks.append({"name": n, "ok": bool(ok), "confidence": conf, "detail": det})

    chk('偏移0 "NCP\\0" 标签', r["label_ok"], "高", data[:4].hex(" "))
    ver = r["version"] or ""
    chk("版本门 前缀∈{01,02}", ver[:2] in ("01", "02"), "高（0x103ba814）", ver)
    chk("总长 0x342 = 834（v1.70 紧凑布局）", r["size"] == TOTAL,
        "高（0x103BB862 序列化器）", f"{r['size']} = 0x{r['size']:X}")
    chk("tag 0x0002 落在 +0xF4", r["curve_tag_off"] == CURVE_TAG_OFF, "高",
        f"0x{(r['curve_tag_off'] or 0):X}")
    chk("曲线表落在 +0x13C", r["table_off"] == TABLE_OFF, "高",
        f"0x{(r['table_off'] or 0):X}")
    blk = next((b for t, b, _ in r["chunks"] if t == CURVE_TAG), None)
    chk("曲线块 len == 0x242", blk is not None and len(blk) == CURVE_LEN, "高（0x103bb6da）",
        f"0x{len(blk or b''):X}")
    c = r["curve"]
    chk("257 点", c is not None and c.size == CURVE_POINTS, "高（0x242=0x40+257×2）",
        f"n={0 if c is None else c.size}")
    if c is not None:
        chk("257 点单调非降", bool(np.all(np.diff(c) >= 0)), "高", f"{c[0]}..{c[-1]}")
        chk("值域 <= 0x7FFF", int(c.max()) <= CURVE_FULL, "高", f"max=0x{c.max():04X}")
        mem = simulate_rev16(c)
        chk("rev16：前 256 字转换后数值不变", bool(np.array_equal(mem[:REV16_WORDS], c[:REV16_WORDS])),
            "高（0x103bc792 movw r4,#0x100）", f"{mem[0]}..{mem[255]}")
        chk("rev16：第 257 字不转换（内存按小端读出=交换值）",
            int(mem[256]) == byteswap(c[256]), "高",
            f"文件 0x{c[256]:04X} -> 内存 0x{mem[256]:04X}")
    scal = [(t, b) for t, b, _ in r["chunks"] if t in SCALAR_TAGS]
    viol = []
    for t, b in scal:
        if t in CONSTRAINTS:
            which, ok, desc = CONSTRAINTS[t]
            got = b[0] if which == "value" else b[1]
            if not ok(got):
                viol.append(f"0x{t:03x} {which}={got} !{desc}")
    chk("标量 tag 恰为 20 个 0x300..0x1600", len(scal) == len(SCALAR_TAGS), "高", f"n={len(scal)}")
    chk("标量约束未违反", not viol, "高（0x103ba832/83a/864）", ";".join(viol) or "无")
    allowed = {VERSION_TAG, NAME_TAG, CURVE_TAG} | set(SCALAR_TAGS)
    extra = sorted({t for t, _, _ in r["chunks"] if t not in allowed})
    chk("只含 v1.70 会写的 tag", not extra, "高（0x103BB862 只写 0x100..0x1600 + 0x2）",
        " ".join(f"0x{t:X}" for t in extra) or "无多余 tag")
    chk("零尾在 +0x33E 且 BE32==0", r["tail_ok"] and len(data) == TAIL_OFF + 4,
        "高", data[TAIL_OFF:TAIL_OFF + 4].hex(" "))
    r["checks"] = checks
    r["rev16_mem"] = simulate_rev16(c) if c is not None else None
    return r


if __name__ == "__main__":
    import sys
    import pipeline as P
    sc, hm = ({}, None)
    if len(sys.argv) > 1:
        sc, hm = scalars_from_extended(open(sys.argv[1], "rb").read())
    blob = build(P.curve_be16_257(), scalars=sc, head_meta=hm)
    rep = parse(blob)
    print(f"总长 {len(blob)} = 0x{len(blob):X}  tag2@0x{rep['curve_tag_off']:X}  "
          f"表@0x{rep['table_off']:X}  尾OK={rep['tail_ok']}")
    mem = simulate_rev16(rep["curve"])
    print(f"rev16 模拟：前 256 字内存值 {mem[0]}..{mem[255]}，第 257 字文件 {rep['curve'][256]} "
          f"-> 内存 {mem[256]}（字节交换，符合固件只转 256 字的行为）")
