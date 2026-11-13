"""ZLOG8 管线自检套件：逐项实测并写入 VERIFY.md。

跑法： python selfcheck.py            # 打印 + 写 VERIFY.md
       python selfcheck.py --quiet    # 只写文件
"""
import argparse
import datetime
import json
import os
import re
import struct
import sys

import numpy as np

import zlog as Z
import pipeline as P
import np3_gen as N

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
R = []


def item(name, values, ok, note=""):
    R.append({"name": name, "values": values, "ok": bool(ok), "note": note})
    return ok


def fmt_num(v):
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return f"{v:.6g}"
    return str(v)


# ---------------- 1. C1 连续性 ----------------
def check_c1():
    dl = Z.K_TOE * Z.P_TOE * Z.X_TOE ** (Z.P_TOE - 1)
    dr = Z.LN_K / Z.X_TOE
    ratio = dl / dr
    item("趾部/主段 C¹ 连续", {"左导数": dl, "右导数": dr, "比值": ratio},
         abs(ratio - 1) < 1e-9, "P_TOE = LN_K/(C_TOE-C_BLACK) 解析导出，非拟合")
    dl2 = Z.K_TOE * Z.P_TOE * (Z.X_TOE * 1.0001) ** (Z.P_TOE - 1)
    dr2 = Z.LN_K / (Z.X_TOE * 1.0001)
    item("交界邻域斜率跳变", {"+0.01%处左": dl2, "+0.01%处右": dr2,
                          "相对跳变": abs(dl2 - dr2) / dr2}, abs(dl2 - dr2) / dr2 < 1e-3,
         "确认不是只在单点吻合")


# ---------------- 2. 解析闭环 ----------------
def check_analytic():
    X = np.geomspace(0.0015, 1.0, 20000)
    e = np.abs(P.zlog8_to_linear(P.linear_to_zlog8(X)) / X - 1)
    item("解析闭环相对误差（未量化）", {"max": e.max(), "median": np.median(e), "p99": np.percentile(e, 99)},
         e.max() < 1e-12, "正逆两式互为解析逆")


# ---------------- 3. 量化闭环 ----------------
def check_quant():
    code = np.arange(256, dtype=np.float64)
    enc = P.curve_lut_256()
    x = np.clip(P.bt709_eotf(code), 0, 1)
    rt = np.clip(P.zlog8_to_linear(enc.astype(np.float64)), 0, 1)
    keep = x > 1e-3
    rel = np.abs(rt[keep] / x[keep] - 1)
    toe = (x <= Z.X_TOE)[keep]
    main = (x > Z.X_TOE)[keep]
    item("量化闭环 主段 (X>0.05)", {"max": rel[main].max(), "median": np.median(rel[main]),
                                "p95": np.percentile(rel[main], 95)}, rel[main].max() < 0.02,
         "8-bit 单码值≈1% 量级，属载体下限")
    item("量化闭环 趾部 (X<=0.05)", {"max": rel[toe].max(), "median": np.median(rel[toe])},
         np.median(rel[toe]) < 0.05,
         "趾部把约 5 档压进 30 级，暗端相对误差必然放大；max=100% 来自最暗一级归入 code 16")


# ---------------- 4. 平台与单调 ----------------
def check_plateaus():
    enc = P.curve_lut_256()
    d = np.diff(enc)
    plat = np.nonzero(d == 0)[0]
    x = np.clip(P.bt709_eotf(np.arange(256, dtype=float)), 0, 1)
    in_toe = int(np.count_nonzero(x[plat] <= Z.X_TOE))
    item("机内曲线单调性", {"递减段": int((d < 0).sum()), "单调非降": bool(np.all(d >= 0))},
         bool(np.all(d >= 0)) and (d < 0).sum() == 0)
    uniq = len(np.unique(enc))
    item("量化平台", {"总数": len(plat), "趾部内": in_toe, "主段内": len(plat) - in_toe,
                   "输入级数": 256, "唯一输出值": uniq, "输出跨度": int(enc.max() - enc.min() + 1)},
         len(plat) == 256 - uniq,
         "恒等式 平台数 = 输入级数 - 唯一输出值数；256 级压进更少级数，平台是必然而非曲线缺陷")


# ---------------- 5. 锚点 ----------------
def check_anchors():
    e = lambda v: float(np.round(np.asarray(Z.encode(np.array([v])))[0], 3))
    gray_code = int(P.curve_lut_256()[int(round(float(P.bt709_oetf(np.array([0.18]))[0])))])
    item("关键锚点", {"黑 pedestal X=0": e(0.0), "趾末 X=0.05": e(Z.X_TOE),
                   "18% 灰卡 X=0.18": e(0.18), "满量程 X=1.0": e(1.0),
                   "机内曲线灰卡落点": gray_code},
         abs(e(Z.X_TOE) - 46) < 1e-6 and abs(e(1.0) - 235) < 1e-6 and abs(gray_code - 127) <= 1)


# ---------------- 6. 每档均匀性 ----------------
def check_stops():
    c = np.arange(46, 236, dtype=float)
    X = P.zlog8_to_linear(c)
    steps = np.diff(np.log2(X))                         # 每 +1 码值的 stop 增量（正值）
    per_stop = 1.0 / np.mean(steps)
    item("对数主段每档码值", {"mean code/stop": per_stop, "阶跃 stop 标准差": float(np.std(steps)),
                          "最大偏离": float(np.abs(steps - np.mean(steps)).max())},
         abs(per_stop - Z.S_STOP) < 0.5, f"SPEC 声明 {Z.S_STOP:.4f} code/stop")


# ---------------- 7. 产物文件 vs 代码 ----------------
def check_files():
    p = os.path.join(OUT, "zlog8_curve_256.txt")
    if os.path.exists(p):
        f = np.loadtxt(p, dtype=np.int64)
        c = P.curve_lut_256()
        item("out/zlog8_curve_256.txt 与代码一致", {"逐点不等数": int(np.count_nonzero(f != c)),
                                               "点数": f.size}, np.array_equal(f, c))
    else:
        item("out/zlog8_curve_256.txt 存在", {"path": p}, False, "先跑 pipeline.write_luts")
    p2 = os.path.join(OUT, "zlog8_curve_257.txt")
    if os.path.exists(p2):
        rows = [l.split() for l in open(p2, encoding="utf-8") if not l.startswith("#") and l.strip()]
        be = np.asarray([int(r[-1]) for r in rows], dtype=np.int64)
        c2 = P.curve_be16_257()
        item("out/zlog8_curve_257.txt 与代码一致",
             {"点数": be.size, "逐点不等数": int(np.count_nonzero(be != c2))
              if be.size == c2.size else "长度不符",
              "值域": f"{be.min() if be.size else '-'}..{be.max() if be.size else '-'}"},
             be.size == N.CURVE_POINTS and np.array_equal(be, c2))
    else:
        item("out/zlog8_curve_257.txt 存在", {"path": p2}, False, "先跑 pipeline.write_luts")


# ---------------- 8. cube vs 解析式 ----------------
def check_cube():
    for name, fn in [("zlog8_to_linear_33.cube", lambda v: np.clip(P.zlog8_to_linear(v), 0, 1)),
                     ("zlog8_to_rec709_33.cube", lambda v: np.clip(P.zlog8_to_rec709(v), 0, 1)),
                     ("zlog8_to_acescg_33.cube", lambda v: P.zlog8_to_aces(v, True)),
                     ("zlog8_to_aces2065_33.cube", lambda v: P.zlog8_to_aces(v, False))]:
        path = os.path.join(OUT, name)
        if not os.path.exists(path):
            item(f"cube 存在: {name}", {}, False)
            continue
        txt = open(path).read().splitlines()
        n = int(next(re.search(r"LUT_3D_SIZE (\d+)", l).group(1) for l in txt if "LUT_3D_SIZE" in l))
        rows = []
        for l in txt:
            parts = l.split()
            if len(parts) != 3:
                continue
            try:
                rows.append([float(t) for t in parts])
            except ValueError:
                continue
        data = np.array(rows, float)
        diag = data[np.arange(n) * (n * n + n + 1), 0]
        vin = np.stack([np.linspace(0, 255, n)] * 3, axis=-1)
        ref = fn(vin)[:, 0]
        err = np.abs(diag - ref).max()
        dmin = float(re.search(r"DOMAIN_MIN (-?[\d.]+)", "\n".join(txt)).group(1))
        lo = float(np.round(np.clip(fn(vin).min(), dmin, 99), 6))
        item(f"cube 对角线 vs 解析式: {name}",
             {"size": n, "条目": data.shape[0], "最大偏差": err, "DOMAIN_MIN": dmin,
              "文件": f"{os.path.getsize(path)/1024:.0f} KiB"},
             data.shape == (n ** 3, 3) and err < 2e-6 and data.min() >= dmin - 1e-9,
             ".cube 用 %.6f，量化下限 1e-6；ACES 两张保留负值不截断")


# ---------------- 9. 色彩矩阵 ----------------
def check_matrix():
    ref_ap0 = np.array([[0.4347, 0.3766, 0.1887], [0.0977, 0.8985, 0.0038], [0.0178, 0.1094, 0.8728]])
    d = np.abs(P.BT709_TO_AP0 - ref_ap0).max()
    item("BT.709->AP0 与官方公开值", {"最大偏差": d}, d < 5e-4, "证明换基算法与原色处理正确")
    rs = np.concatenate([P.BT709_TO_AP1.sum(1), P.BT709_TO_AP0.sum(1)])
    item("换基矩阵行和=1（D65 白点不变）", {"AP1": P.BT709_TO_AP1.sum(1).tolist(),
                                     "AP0": P.BT709_TO_AP0.sum(1).tolist()}, np.abs(rs - 1).max() < 1e-9)
    w = P.BT709.sum(axis=1)
    item("由原色重算的 D65 白点 XYZ", {"X": w[0], "Y": w[1], "Z": w[2]},
         np.abs(w - np.array([0.9505, 1.0, 1.0890])).max() < 1e-3)
    inv = np.linalg.inv(P.BT709_TO_AP1) @ P.BT709_TO_AP1
    item("AP1 变换往返误差", {"max|inv(M)*M - I|": np.abs(inv - np.eye(3)).max()},
         np.abs(inv - np.eye(3)).max() < 1e-12)


# ---------------- 10. NP3 闭环（真实容器模型） ----------------
def _template():
    if not os.path.exists(N.DEFAULT_SAMPLE):
        return None, None, None
    srep = N.parse(open(N.DEFAULT_SAMPLE, "rb").read())
    chunks = [(c["tag"], c["body"]) for c in srep["chunks"] if c["tag"] != N.CURVE_TAG]
    return srep, srep["curve_head"], chunks


def check_np3():
    curve = P.curve_be16_257()
    srep, head_tmpl, chunks = _template()
    if srep is None:
        item("真实样本可用", {"path": N.DEFAULT_SAMPLE}, False, "样本缺失，无法定标")
        return
    bad_s = [c["name"] for c in srep["checks"] if not c["ok"]]
    item("真实样本 PICCON219.NP3 解析",
         {"字节": srep["size"], "version": srep["version"], "name": srep["name"],
          "点数": int(srep["curve"].size), "曲线": f"{srep['curve'].min()}..{srep['curve'].max()}",
          "tags": len(srep["chunks"]), "未通过": bad_s or "无"},
         not bad_s, "标签/版本门/块长/单调/值域/尾部全过")
    item("样本值域落在 0x7FFF 内",
         {"max": f"0x{int(srep['curve'].max()):04X}", "CURVE_FULL": hex(N.CURVE_FULL)},
         int(srep["curve"].max()) <= N.CURVE_FULL and int(srep["curve"].min()) >= 0)
    item("块长恒等式 0x242 = 0x40 + 257*2",
         {"CURVE_LEN": N.CURVE_LEN, "计算": N.CURVE_HDR + N.CURVE_POINTS * 2},
         N.CURVE_LEN == N.CURVE_HDR + N.CURVE_POINTS * 2,
         "解释了旧模型里『块尾余出 2 字节』——那是第 257 个点")

    blob, ordered = N.build(curve, template_chunks=chunks, head_tmpl=head_tmpl)
    rep = N.parse(blob)
    bad = [c["name"] for c in rep["checks"] if not c["ok"]]
    rej = [l for l in rep["loader"] if l[0].startswith("拒绝")]
    item("ZLOG8.NP3 生成后读回",
         {"字节": len(blob), "tags": len(rep["chunks"]), "未通过": bad or "无",
          "loader拒绝": [f"0x{t:05x}" for _, t, _ in rej] or "无"},
         not bad and not rej,
         "按真实容器模型 + 固件 tag 规则模拟")
    same = rep["curve"] is not None and np.array_equal(rep["curve"], curve)
    item("自产物曲线逐点还原", {"一致": bool(same), "点数": int(rep["curve"].size)}, same,
         "BE16 大端写出 -> 大端读回")
    item("自产物块头与样本一致",
         {"字节相同": bytes(rep["curve_head"]) == bytes(head_tmpl)},
         bytes(rep["curve_head"]) == bytes(head_tmpl), "复用真实样本 64B 块头")
    item("第三方版权文本已替换",
         {"自产物 0x10100": next((b[:24].decode('latin-1') for t, b in
                                  [(c['tag'], c['body']) for c in rep['chunks']] if t == N.DESC_TAG), None),
          "样本 0x10100": next((b[:24].decode('latin-1') for t, b in chunks if t == N.DESC_TAG), None)},
         all(t != N.DESC_TAG or not b.startswith(b"A B&W") for t, b in
             [(c["tag"], c["body"]) for c in rep["chunks"]]),
         "不得把他人的版权说明带进我们的产物")
    item("ZLOG8 曲线灰卡在 15-bit 域的位置",
         {"norm@X=0.18": round(float(np.interp(126.8, curve, np.arange(257) / 256.0)), 5),
          "BE16 中段": int(curve[128]), "值域": f"{int(curve.min())}..{int(curve.max())}"},
         int(curve.max()) <= N.CURVE_FULL and bool(np.all(np.diff(curve) >= 0)))


def check_cross_read():
    """用 color 线的参考解析器读我们的产物（互读验证容器模型）。"""
    # 样本 .../work/color/tmpout/card/x.NP3 -> 去 3 层 = .../work/color
    color_dir = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(N.DEFAULT_SAMPLE))))
    try:
        # 从样本路径反推 color 线目录：work/color/tmpout/card/x.NP3 -> work/color
        sys.path.insert(0, color_dir)
        import z6ii_np3 as REF
        del sys.path[0]
    except Exception as e:
        item("color 线参考解析器可导入", {"err": str(e), "dir": color_dir}, False,
             "只读引用，不修改它")
        return
    curve = P.curve_be16_257()
    srep, head_tmpl, chunks = _template()
    blob, _ = N.build(curve, template_chunks=chunks, head_tmpl=head_tmpl)
    try:
        ver, name, cs = REF.parse(blob)
    except Exception as e:
        item("互读：参考解析器读自产物", {"err": str(e)}, False)
        return
    tags = [t for t, _ in cs]
    ref_curve = None
    for t, b in cs:
        if t == REF.TONE_CURVE_TAG:
            ref_curve = struct.unpack(">257H", b[REF.TONE_CURVE_HEADER:
                                               REF.TONE_CURVE_HEADER + 257 * 2])
    item("互读：参考解析器读自产物",
         {"version": ver, "name": name, "tags": len(cs), "曲线 tag 在末": tags[-1] == REF.TONE_CURVE_TAG,
          "点数": len(ref_curve or ()), "逐点一致": bool(ref_curve and np.array_equal(
              np.asarray(ref_curve, np.int64), curve))},
         ver == "0210" and name == "ZLOG8" and tags[-1] == REF.TONE_CURVE_TAG
         and ref_curve is not None and np.array_equal(np.asarray(ref_curve, np.int64), curve),
         "两套独立实现解析同一文件，结论相同")
    # 反向：参考解析器读真实样本，与我们的解析比对
    r2, n2, c2 = REF.parse(open(N.DEFAULT_SAMPLE, "rb").read())
    item("互读：参考解析器读真实样本",
         {"version": r2, "name": n2, "tags": len(c2)}, r2 == srep["version"] and n2 == srep["name"])


# ---------------- 11. SPEC 与代码一致 ----------------
def check_spec():
    p = os.path.join(HERE, "SPEC.md")
    if not os.path.exists(p):
        item("SPEC.md 存在", {}, False)
        return
    s = open(p, encoding="utf-8").read()
    need = {"16337.2": f"{Z.K_TOE:.1f}", "2.1030": f"{Z.P_TOE:.4f}",
            "63.0898": f"{Z.LN_K:.4f}", "43.73": f"{Z.S_STOP:.2f}",
            "127": str(int(P.curve_lut_256()[int(round(float(P.bt709_oetf(np.array([0.18]))[0])))])),
            "0x342": "原生 834 B 布局", "0x10020000": "运行期基址口径",
            "0x103DB7B4": "断点用运行期地址"}
    miss = {k: v for k, v in need.items() if k not in s}
    item("SPEC.md 常数与代码一致", {"缺失项": miss or "无", "核对数": len(need)}, not miss)


def check_native_layout():
    """v1.70 原生紧凑布局 0x342=834：生成、读回、rev16 语义、偏移自洽。"""
    try:
        import z6ii_np3_real as REAL
    except Exception as e:
        item("z6ii_np3_real 可导入", {"err": str(e)}, False)
        return
    curve = P.curve_be16_257()
    sc, hm = ({}, None)
    if os.path.exists(N.DEFAULT_SAMPLE):
        sc, hm = REAL.scalars_from_extended(open(N.DEFAULT_SAMPLE, "rb").read())
    blob = REAL.build(curve, scalars=sc, head_meta=hm)
    rep = REAL.audit(blob)
    bad = [c["name"] for c in rep["checks"] if not c["ok"]]
    item("原生 834B 布局生成与读回",
         {"字节": len(blob), "hex": hex(len(blob)), "审计项": len(rep["checks"]), "未通过": bad or "无",
          "tag2@": f"0x{(rep['curve_tag_off'] or 0):X}", "表@": f"0x{(rep['table_off'] or 0):X}"},
         not bad, "序列化器 0x103BB862 紧凑布局；0xF4/0xFC/0x13C/0x33E 全部实测吻合")
    m = rep["rev16_mem"]
    item("rev16 只转前 256 字（第 257 点保持 BE 字节）",
         {"前256一致": bool(np.array_equal(m[:256], curve[:256])),
          "第257 文件": f"0x{curve[256]:04X}", "第257 内存": f"0x{m[256]:04X}"},
         bool(np.array_equal(m[:256], curve[:256])) and int(m[256]) == REAL.byteswap(int(curve[256])),
         "0x103bc792 `movw r4,#0x100` 限定 256 次循环；主机侧还原第 257 点须自行按 BE 取值")
    off = 4 + 12 + 28 + len(REAL.SCALAR_TAGS) * 10
    item("原生布局偏移算术自洽",
         {"标量tag数": len(REAL.SCALAR_TAGS), "tag2偏移": f"0x{off:X}", "期望": "0xF4",
          "总长": off + 8 + REAL.CURVE_LEN + 4, "期望总长": REAL.TOTAL},
         off == REAL.CURVE_TAG_OFF and off + 8 + REAL.CURVE_LEN + 4 == REAL.TOTAL,
         "(0x1600-0x300)/0x100+1 = 20；4+12+28+200 = 244 = 0xF4")
    # 扩展样本按 TLV 扫描同样能被读回（读取器不依赖布局长度）
    if os.path.exists(N.DEFAULT_SAMPLE):
        ex = REAL.parse(open(N.DEFAULT_SAMPLE, "rb").read())
        item("扩展 1234B 样本按 TLV 仍可定位 tag2",
             {"size": ex["size"], "tag2@": f"0x{(ex['curve_tag_off'] or 0):X}",
              "表@": f"0x{(ex['table_off'] or 0):X}", "点数": int(ex["curve"].size)},
             ex["curve"] is not None and ex["curve"].size == REAL.CURVE_POINTS,
             "读取器按 TLV 扫描找 tag 2，故两种布局都能载入；产物默认用原生")


def write_verify():
    lines = [f"# ZLOG8 管线自检报告", "",
             f"生成时间：{datetime.date.today().isoformat()}　命令：`python selfcheck.py`",
             f"环境：numpy {np.__version__}　定义源：zlog.py（解析式，非拟合）", ""]
    npass = sum(1 for r in R if r["ok"])
    lines += [f"**{npass}/{len(R)} 项通过**", "", "| 项 | 实测值 | 判定 | 说明 |", "| --- | --- | --- | --- |"]
    for r in R:
        v = "<br>".join(f"{k}={fmt_num(x)}" for k, x in r["values"].items())
        lines.append(f"| {r['name']} | {v} | {'PASS' if r['ok'] else 'FAIL'} | {r['note']} |")
    lines += ["", "## 结论边界", "",
              "- 以上全部是**主机侧实测**，可在无相机、无 QEMU 下复现。",
              "- NP3 的文件头偏移、版本取值、BE16 值域与尾部 2 字节仍是静态推断，"
              "**必须有一个相机导出的真实 NP3 才能定标**。",
              "- 曲线是否作用于视频录制路径，需相机实机或 QEMU 挂载 NP3 后验证。", ""]
    path = os.path.join(HERE, "VERIFY.md")
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return path, npass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    for fn in (check_c1, check_analytic, check_quant, check_plateaus, check_anchors,
               check_stops, check_files, check_cube, check_matrix, check_np3,
               check_cross_read, check_native_layout, check_spec):
        fn()
    path, npass = write_verify()
    for r in R:
        if not a.quiet or not r["ok"]:
            print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}")
            for k, v in r["values"].items():
                print(f"        {k:22s} = {fmt_num(v)}")
    print(f"\n{npass}/{len(R)} 通过 -> {path}")
    return 0 if npass == len(R) else 1


if __name__ == "__main__":
    sys.exit(main())
