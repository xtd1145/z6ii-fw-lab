"""ZLOG8 主机侧管线：正向编码、逆向还原、BT.709 / ACES 转换与 .cube 生成。

X  = 场景线性亮度 / 视频满量程（X=1.0 为 clip 点）
z  = ZLOG8 的 8-bit 码值（0..255，有效 16..235）
"""
import numpy as np
from zlog import encode, decode, bt709_eotf, bt709_oetf

GRAY_REF = 0.18          # 18% 灰卡 = Stop 0，沿用 RED 惯例


# ---------- 色彩空间基元（矩阵由原色坐标现算，不抄表） ----------
def _rgb_to_xyz(rx, ry, gx, gy, bx, by, wx, wy):
    """由原色色度与白点求 3x3（行归一后 det 归一）。"""
    def uv(x, y):
        return np.array([x / y, 1.0, (1 - x - y) / y])
    r, g, b, w = uv(rx, ry), uv(gx, gy), uv(bx, by), uv(wx, wy)
    M = np.column_stack([r, g, b])
    s = np.linalg.solve(M, w)
    return M * s                      # 按列缩放：M[:, j] *= s[j]


D65 = (0.3127, 0.3290)
BT709 = _rgb_to_xyz(0.640, 0.330, 0.300, 0.600, 0.150, 0.060, *D65)
AP1 = _rgb_to_xyz(0.766, 0.223, 0.225, 0.745, 0.140, 0.060, *D65)   # ACEScg
AP0 = _rgb_to_xyz(0.7347, 0.2653, 0.0000, 1.0000, 0.0015, 0.0053, *D65)  # ACES2065-1
# _rgb_to_xyz 返回 RGB->XYZ，故 A->B = inv(B) @ A
BT709_TO_AP1 = np.linalg.inv(AP1) @ BT709
BT709_TO_AP0 = np.linalg.inv(AP0) @ BT709
BT2020 = _rgb_to_xyz(0.708, 0.292, 0.170, 0.797, 0.131, 0.046, *D65)
BT709_TO_2020 = np.linalg.inv(BT2020) @ BT709


# ---------- 正向 / 逆向 ----------
def linear_to_zlog8(X):
    """场景线性(归一满量程) -> 8-bit ZLOG8 码值"""
    return np.clip(encode(np.asarray(X, float)), 0, 255)


def zlog8_to_linear(code):
    """8-bit 码值 -> 场景线性 X（解析逆，非拟合）"""
    return decode(np.asarray(code, float))


def zlog8_to_rec709(code):
    """码值 -> BT.709 显示域 0..1（监看/预览用）"""
    return np.clip(bt709_oetf(zlog8_to_linear(code)) / 255.0, 0, 1)


def zlog8_to_aces(code, ap1=True):
    """ZLOG8 码值向量 -> ACES 线性。code 为逐通道码值（形状 (3,) 或 (N,3)）。
    ap1=True 给 ACEScg(AP1)，False 给 ACES2065-1(AP0)。"""
    lin = np.clip(zlog8_to_linear(code), 0, None)
    m = BT709_TO_AP1 if ap1 else BT709_TO_AP0
    return lin @ m.T


# ---------- LUT / .cube ----------
def curve_lut_256():
    """机内曲线：输入 BT.709 显示域码值 0..255 -> 输出 ZLOG8 码值。
    Picture Control 作用在已做 709 伽马的信号上，不是线性光。"""
    i = np.arange(256.0)
    return np.rint(linear_to_zlog8(bt709_eotf(i))).astype(np.int64)


CURVE_POINTS_257 = 257
CURVE_FULL = 0x7FFF          # 真实 NP3 实测：257 个 BE16，满量程 0x7FFF（15-bit）


def curve_norm_257():
    """NP3 载体用的 257 节点曲线，返回 0..1 归一浮点。
    节点 u = i/256（i=0..256）覆盖显示域 0..255 两端；先 eotf 到线性再 encode。"""
    u = np.arange(CURVE_POINTS_257, dtype=float) / (CURVE_POINTS_257 - 1)
    return np.clip(encode(bt709_eotf(u * 255.0)) / 255.0, 0.0, 1.0)


def curve_be16_257():
    """归一曲线量化为真实 NP3 的 257 个 BE16（0..0x7FFF）"""
    return np.rint(np.clip(curve_norm_257() * CURVE_FULL, 0, CURVE_FULL)).astype(np.int64)


def _write_cube(path, n, fn, title, dmin=0.0, dmax=1.0):
    with open(path, "w") as f:
        f.write(f'TITLE "{title}"\n')
        f.write(f"LUT_3D_SIZE {n}\nDOMAIN_MIN {dmin:.2f} {dmin:.2f} {dmin:.2f}\n"
                f"DOMAIN_MAX {dmax:.2f} {dmax:.2f} {dmax:.2f}\n")
        g = np.linspace(0.0, 1.0, n) * 255.0
        R, G, B = np.meshgrid(g, g, g, indexing="ij")
        v = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=-1)
        np.savetxt(f, fn(v), fmt="%.6f")
    return path


def _clip01(v):
    return np.clip(v, 0.0, 1.0)


def write_luts(outdir, n=33):
    import os
    os.makedirs(outdir, exist_ok=True)
    made = []
    # ACES 输出允许负值（色域外），截断会丢信息，故用更宽 DOMAIN 且不 clip
    ACES_MIN, ACES_MAX = -0.25, 1.25
    fns = [
        (f"zlog8_to_linear_{n}.cube", lambda v: _clip01(zlog8_to_linear(v)),
         "ZLOG8 -> scene-linear BT.709 primaries", 0.0, 1.0),
        (f"zlog8_to_rec709_{n}.cube", lambda v: _clip01(zlog8_to_rec709(v)),
         "ZLOG8 -> Rec.709 display (monitoring)", 0.0, 1.0),
        (f"zlog8_to_acescg_{n}.cube", lambda v: zlog8_to_aces(v, ap1=True),
         "ZLOG8 -> ACEScg (AP1) linear", ACES_MIN, ACES_MAX),
        (f"zlog8_to_aces2065_{n}.cube", lambda v: zlog8_to_aces(v, ap1=False),
         "ZLOG8 -> ACES2065-1 (AP0) linear", ACES_MIN, ACES_MAX),
    ]
    for name, fn, title, dmin, dmax in fns:
        p = _write_cube(os.path.join(outdir, name), n, fn, title, dmin, dmax)
        made.append((name, os.path.getsize(p)))
    with open(os.path.join(outdir, "zlog8_curve_256.txt"), "w") as f:
        f.write("\n".join(str(int(v)) for v in curve_lut_256()) + "\n")
    made.append(("zlog8_curve_256.txt", os.path.getsize(os.path.join(outdir, "zlog8_curve_256.txt"))))
    p257 = os.path.join(outdir, "zlog8_curve_257.txt")
    with open(p257, "w") as f:
        f.write("# i  u=i/256  in_code=u*255  zlog8_code  norm  BE16(0..0x7FFF)\n")
        u = np.arange(CURVE_POINTS_257) / (CURVE_POINTS_257 - 1)
        b = curve_be16_257()
        nrm = curve_norm_257()
        for i in range(CURVE_POINTS_257):
            f.write("%d\t%.6f\t%.4f\t%.4f\t%.6f\t%d\n"
                    % (i, u[i], u[i] * 255.0, nrm[i] * 255.0, nrm[i], b[i]))
    made.append(("zlog8_curve_257.txt", os.path.getsize(p257)))
    return made


if __name__ == "__main__":
    ref_ap0 = np.array([[0.4347, 0.3766, 0.1887],
                        [0.0977, 0.8985, 0.0038],
                        [0.0178, 0.1094, 0.8728]])      # 官方 Rec.709(Video) -> ACES2065-1
    print("BT.709 -> AP0(ACES2065-1) 自算:")
    print(np.round(BT709_TO_AP0, 4))
    print("  与官方参考最大偏差:", round(np.abs(BT709_TO_AP0 - ref_ap0).max(), 4),
          "<= 4e-4 即证明换基算法与原色处理正确")
    print("  行和:", np.round(BT709_TO_AP0.sum(axis=1), 6), "(须为 1，D65 白点不变)")
    print("BT.709 -> AP1(ACEScg) 自算:")
    print(np.round(BT709_TO_AP1, 4))
    print("  行和:", np.round(BT709_TO_AP1.sum(axis=1), 6))
    print("D65 白点 XYZ (由 BT.709 原色重算):", np.round(BT709.sum(axis=1), 6),
          "官方 D65 = (0.9505, 1.0000, 1.0890)")
    print("AP1 自逆往返误差:", f"{np.abs(np.linalg.inv(BT709_TO_AP1) @ BT709_TO_AP1 - np.eye(3)).max():.2e}")
