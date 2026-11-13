"""ZLOG8 —— 仿 R3D/REDlog 三段哲学、针对 Z6 II 8-bit H.264 内录标定的对数格式"""
import numpy as np

C_BLACK, X_TOE, C_TOE, C_TOP = 16.0, 0.05, 46.0, 235.0
LN_K   = (C_TOP - C_TOE) / np.log(C_TOE / X_TOE) * 1.0     # ln 主段系数，先由两段交界反解
LN_K   = (C_TOP - C_TOE) / np.log(1.0 / X_TOE)             # 63.0885
P_TOE  = LN_K / (C_TOE - C_BLACK)                          # C1 连续条件 => 2.10295
K_TOE  = (C_TOE - C_BLACK) / X_TOE ** P_TOE
S_STOP = LN_K * np.log(2.0)                                # code per stop

def encode(X):
    X = np.asarray(X, float)
    return np.clip(np.where(X <= X_TOE, C_BLACK + K_TOE * np.power(np.maximum(X, 0), P_TOE),
                                           LN_K * np.log(np.maximum(X, 1e-12)) + C_TOP), 0, 255)

def decode(x):
    x = np.clip(np.asarray(x, float), 0, 255)
    return np.where(x <= C_TOE, np.maximum(((x - C_BLACK) / K_TOE), 0) ** (1 / P_TOE),
                    np.exp((x - C_TOP) / LN_K))

def bt709_eotf(e):
    e = np.asarray(e, float) / 255.0
    return np.where(e < 0.0180539685, e / 4.5, ((e + 0.0992968593) / 1.0992968593) ** (1 / 0.45))


def bt709_oetf(L):
    """BT.709 OETF：线性 X -> 显示域码值 0..255（监看还原用，eotf 的解析逆）"""
    L = np.asarray(L, float)
    return np.where(L < 0.00186, L * 4.5,
                    1.0992968593 * np.power(np.maximum(L, 0.0), 0.45) - 0.0992968593) * 255.0

if __name__ == "__main__":
    d_left  = K_TOE * P_TOE * X_TOE ** (P_TOE - 1)
    d_right = LN_K / X_TOE
    print("=== ZLOG8 常数")
    print(f"  LN_K   = {LN_K:.6f}      S_STOP = {S_STOP:.6f} code/stop")
    print(f"  P_TOE  = {P_TOE:.6f}      K_TOE  = {K_TOE:.4f}")
    print(f"  C1 连续性: 趾末左导数 {d_left:.4f}  主段右导数 {d_right:.4f}  比值 {d_left/d_right:.6f}")
    print("\n=== 锚点（Stop 以 18% 灰卡 = X 0.18 为零点）")
    for lbl, X in [("黑 pedestal", 0.0), ("X=0.002", 0.002), ("趾末 X=0.05", X_TOE),
                   ("18% 灰卡", 0.18), ("X=0.30", 0.30), ("X=0.70", 0.70), ("满量程 X=1.0", 1.0)]:
        c = float(encode(np.array([X]))[0]); st = np.log2(X / 0.18) if X > 0 else -99
        print(f"  {lbl:14s} X={X:<7.4g} stop {st:+6.2f}   code {c:6.1f}  IRE {c/255*100:5.1f}%")
    i = np.arange(256.0); ci = np.rint(encode(bt709_eotf(i))).astype(int); dd = np.diff(ci)
    print(f"\n=== 机内 256 点曲线  min={ci.min()} max={ci.max()} 单调非降={bool(np.all(dd>=0))} 平台={int((dd==0).sum())} 递减={int((dd<0).sum())}")
    print(f"  curve[0:8]={list(map(int,ci[:8]))}  curve[120:130]={list(map(int,ci[120:130]))}  curve[250:]={list(map(int,ci[250:]))}")
    X = np.geomspace(0.0015, 1.0, 8000); err = np.abs(decode(encode(X)) / X - 1)
    print(f"\n=== 还原闭环误差  max={err.max()*100:.4f}%  median={np.median(err)*100:.6f}%")
    for cut in (0.05, 0.18, 0.5):
        print(f"  X>={cut}: max {err[X>=cut].max()*100:.4f}%")
    print(f"\n=== 量化预算  主段 {np.log2(1/X_TOE):.2f} 档 @ {S_STOP:.2f} code/档；总覆盖 {np.log2(1/0.0015):.1f} 档")
    print("  本脚本不落盘。产物唯一写源是 pipeline.write_luts()，避免同名 cube 被 1D 版覆盖。")
