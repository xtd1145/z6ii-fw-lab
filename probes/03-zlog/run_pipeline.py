"""ZLOG8 主机侧批处理：把机内拍出的 8-bit ZLOG8 影像还原，或反向模拟机内编码。

用法
  python run_pipeline.py decode in.png            # -> in.rec709.png (监看)
  python run_pipeline.py decode imgs/ --mode linear --bits 16
  python run_pipeline.py encode gray.png --bits 8  # 线性/显示域 -> ZLOG8
  python run_pipeline.py selftest                  # 无外部输入跑通全链并打印误差
"""
import argparse
import glob
import os
import sys

import numpy as np

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

import pipeline as P


def read_image(path):
    """返回 float64 数组，码值域 0..255（8-bit 输入）。"""
    if path.lower().endswith(".npy"):
        a = np.load(path).astype(np.float64)
        return a * 255.0 if a.max() <= 1.0 else a
    if not HAVE_PIL:
        raise SystemExit("需要 Pillow 才能读普通图像：pip install pillow")
    im = Image.open(path)
    a = np.asarray(im.convert("RGB"), dtype=np.float64)
    if a.max() > 255.0:
        a = a / (a.max() / 255.0)
    return a


def write_out(path, arr, bits, asfloat=False):
    stem, _ = os.path.splitext(path)
    if asfloat:
        np.save(stem + ".npy", arr.astype(np.float32))
        return stem + ".npy"
    if bits == 8:
        p = stem + ".png"
        Image.fromarray(np.uint8(np.clip(arr, 0, 255))).save(p)
    else:
        p = stem + ".tif"
        Image.fromarray(np.uint16(np.clip(arr, 0, 65535)), mode="I;16").save(p)
    return p


def process(files, mode, bits, outdir):
    rows = []
    for f in files:
        v = read_image(f)
        tgt = os.path.join(outdir, os.path.basename(f)) if outdir else f
        if mode == "decode":
            lin = np.clip(P.zlog8_to_linear(v), 0, 1)
            if bits == 8:
                o, b, f32 = P.zlog8_to_rec709(v) * 255.0, 8, False
            else:
                o, b, f32 = lin * (65535.0 if bits == 16 else 1.0), bits, bits == 32
        elif mode == "aces":
            o, b, f32 = P.zlog8_to_aces(v, ap1=(bits != 0)), 32, True
        elif mode == "encode":
            x = np.clip(v / 255.0, 0, 1)
            o, b, f32 = P.linear_to_zlog8(P.bt709_eotf(v) if bits == 709 else x), 8, False
        else:
            raise SystemExit(f"未知 mode {mode}")
        p = write_out(tgt, o, b, f32)
        rows.append((f, p, o.shape, float(np.min(o)), float(np.max(o))))
    return rows


def selftest():
    """合成灰度楔形 -> 机内曲线量化 -> 还原，测端到端误差。"""
    import zlog as Z
    code = np.arange(256, dtype=np.float64)
    enc = P.curve_lut_256()                        # 机内曲线：显示域 -> ZLOG8
    x = np.clip(P.bt709_eotf(code), 0, 1)          # 显示域 -> 线性
    rt = np.clip(P.zlog8_to_linear(enc.astype(np.float64)), 0, 1)
    keep = x > 1e-3
    rel = np.abs(rt[keep] / x[keep] - 1)
    toe_m, main_m = (x <= Z.X_TOE)[keep], (x > Z.X_TOE)[keep]
    Xg = np.geomspace(0.0015, 1.0, 4000)
    an = np.abs(P.zlog8_to_linear(P.linear_to_zlog8(Xg)) / Xg - 1)
    d = np.diff(enc)
    plat = np.nonzero(d == 0)[0]
    gray = int(enc[int(round(float(P.bt709_oetf(np.array([0.18]))[0])))])
    print("[selftest] 机内曲线 256 点 -> ZLOG8 码值")
    print(f"  单调非降: {bool(np.all(d >= 0))}   范围 {enc.min()}..{enc.max()}   18% 灰卡 -> code {gray}")
    print(f"  平台数 {len(plat)}；其中位于趾部(X<={Z.X_TOE}) {int(np.count_nonzero(x[plat] <= Z.X_TOE))} 个")
    print(f"  解析闭环（未量化）相对误差 max={an.max()*100:.6f}%")
    print(f"  量化闭环 主段 max={rel[main_m].max()*100:.3f}% median={np.median(rel[main_m])*100:.3f}%")
    print(f"  量化闭环 趾部 max={rel[toe_m].max()*100:.3f}% median={np.median(rel[toe_m])*100:.3f}%")
    back = np.clip(P.zlog8_to_rec709(enc.astype(np.float64)), 0, 1) * 255.0
    print(f"  709 域往返偏差 max={np.abs(back-code).max():.2f} code"
          "（参考值，非判据：Log 就是把 256 级重新分配，本就非无损）")
    ok = (bool(np.all(d >= 0)) and abs(gray - 127) <= 1 and an.max() < 1e-9
          and rel[main_m].max() < 0.02)
    print("[selftest]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["decode", "encode", "aces", "selftest"])
    ap.add_argument("src", nargs="?", help="输入文件或目录")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--bits", type=int, default=8, help="8=png 监看 / 16=tiff 线性 / 32=npy float / 709=encode 输入按709域")
    ap.add_argument("--outdir", default="out/frames")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    if not a.src:
        raise SystemExit("需要输入文件或目录")
    if os.path.isdir(a.src):
        files = sorted(glob.glob(os.path.join(a.src, "*.png")) + glob.glob(os.path.join(a.src, "*.tif"))
                       + glob.glob(os.path.join(a.src, "*.jpg")) + glob.glob(os.path.join(a.src, "*.npy")))
    else:
        files = [a.src]
    if not files:
        raise SystemExit(f"{a.src} 下没有可读图像")
    os.makedirs(a.outdir, exist_ok=True)
    rows = process(files, a.cmd, a.bits, a.outdir)
    print(f"[{a.cmd}] {len(rows)} 帧 -> {a.outdir}")
    for src, dst, shape, lo, hi in rows[:12]:
        print(f"  {os.path.basename(src):22s} -> {os.path.basename(dst):24s} "
              f"{str(shape):14s} {lo:8.1f}..{hi:8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
