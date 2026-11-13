import numpy as np, os, sys
sys.path.insert(0,"work/zlog")
from zlog import encode, decode, bt709_eotf, LN_K, P_TOE, S_STOP, X_TOE, C_TOE, C_TOP, C_BLACK

OUT = os.path.expanduser(r"~\.qoder\app\chat-session-visualizations\0d060783-3e1d-4a58-aa4d-a0e45a652cd5\visualizations")
os.makedirs(OUT, exist_ok=True)

st = np.linspace(-7, 2.5, 400)
X  = 0.18 * 2**st
zlog = np.clip(encode(X), 0, 255) / 255.0
def nlog_fwd(y):
    y=np.clip(y,0,None); lo=650*np.power(y+0.0075,1/3); hi=150*np.log(np.where(y>0,y,1e-12))+619
    return np.where(y<0.328, lo, hi)
nlog = np.clip(nlog_fwd(X), 0, 1023) / 1023.0
b709 = np.clip(bt709_eotf(np.clip(X,0,1)*255), 0, 255) / 255.0

def path(vals, x0=-7, x1=2.5, W=760, H=340, pad=44):
    pts=[]
    for s,v in zip(st,vals):
        px = pad + (s-x0)/(x1-x0)*(W-2*pad)
        py = H-pad - v*(H-2*pad)
        pts.append(f"{px:.1f},{py:.1f}")
    return "M"+" L".join(pts)
W,H,pad=760,340,44
def sx(s): return pad+(s+7)/9.5*(W-2*pad)
def sy(v): return H-pad-v*(H-2*pad)
grid="".join(f'<line x1="{sx(s):.1f}" y1="{sy(0):.0f}" x2="{sx(s):.1f}" y2="{sy(1):.0f}" stroke="#20262e"/>' for s in range(-7,3))
grid+="".join(f'<line x1="{sx(-7):.0f}" y1="{sy(v/4):.1f}" x2="{sx(2.5):.0f}" y2="{sy(v/4):.1f}" stroke="#20262e"/>' for v in range(0,5,1))
xlab="".join(f'<text x="{sx(s):.1f}" y="{H-16}" fill="#7d8896" font-size="11" text-anchor="middle">{s:+d}</text>' for s in range(-7,3))
ylab="".join(f'<text x="{pad-10:.0f}" y="{sy(v/4)+4:.1f}" fill="#7d8896" font-size="11" text-anchor="end">{int(v*25)}</text>' for v in range(0,5))
zero=f'<line x1="{sx(0):.1f}" y1="{sy(0):.0f}" x2="{sx(0):.1f}" y2="{sy(1):.0f}" stroke="#4a5568" stroke-dasharray="4 3"/>'
marks=[(0.0, np.clip(encode(np.array([0.18]))[0],0,255)/255, "18% 灰卡 · code 127"),
       (np.log2(0.05/0.18), C_TOE/255, "趾末 · code 46"),
       (np.log2(1/0.18), C_TOP/255, "满量程 · code 235")]
dots="".join(f'<circle cx="{sx(a):.1f}" cy="{sy(b):.1f}" r="4" fill="#f0a63c"/><text x="{sx(a)+9:.1f}" y="{sy(b)-7:.1f}" fill="#f0a63c" font-size="11">{c}</text>' for a,b,c in marks)

html = f"""<style>
:root{{--bg:#12161b;--fg:#dfe5ec;--mut:#8b97a6;--card:#1a2028;--line:#2a323c;--acc:#f0a63c;--z:#3fb27f;--n:#c86bda;--b:#5aa9e6}}
*{{box-sizing:border-box}}body{{margin:0;color:var(--fg);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:880px;padding:18px 4px 26px;background:var(--bg);border-radius:12px}}
h1{{font-size:21px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}}
.kpi{{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}}
.kpi div{{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.kpi b{{display:block;font-size:22px;color:var(--acc);font-weight:600}}
.kpi span{{color:var(--mut);font-size:12px}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 12px;font-size:13px}}
.legend i{{display:inline-block;width:22px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}}
code{{background:#0d1116;border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:12.5px;color:#cfe3d8}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--mut);font-weight:600}}td.n{{font-variant-numeric:tabular-nums}}
.note{{color:var(--mut);font-size:12.5px;margin-top:8px}}
.warn{{border-left:3px solid var(--acc);padding-left:12px;color:#e9dcc3}}
</style><div class="wrap">
<h1>ZLOG8 · 8-bit 机内对数格式</h1>
<div class="sub">仿 R3D/REDlog 结构（幂 toe + 对数主段 + 18% 灰卡参考），按 Z6 II 的 8-bit H.264 载体重新标定 · v0.1.0</div>
<div class="kpi">
<div><b>43.73</b><span>主段 code / 档</span></div>
<div><b>127</b><span>18% 灰卡 code (50% IRE)</span></div>
<div><b>9.4 档</b><span>有效动态范围</span></div>
<div><b>66</b><span>量化平台（原 N-Log 直译 149）</span></div>
</div>
<div class="card">
<div class="legend">
<span><i style="background:var(--z)"></i>ZLOG8（8-bit）</span>
<span><i style="background:var(--n)"></i>N-Log v1.0.0（10-bit）</span>
<span><i style="background:var(--b)"></i>BT.709 原生</span>
</div>
<svg viewBox="0 0 {W} {H+24}" width="100%" role="img" aria-label="ZLOG8 与 N-Log、BT.709 曲线对比">
{grid}{zero}
<path d="{path(b709)}" fill="none" stroke="#5aa9e6" stroke-width="2"/>
<path d="{path(nlog)}" fill="none" stroke="#c86bda" stroke-width="2" stroke-dasharray="6 4"/>
<path d="{path(zlog)}" fill="none" stroke="#3fb27f" stroke-width="2.6"/>
{dots}{xlab}{ylab}
<text x="{sx(-7):.0f}" y="{H-2}" fill="#7d8896" font-size="11">← Stop（以 18% 灰卡 = 0）</text>
<text x="{pad-34:.0f}" y="18" fill="#7d8896" font-size="11">归一化码值</text>
</svg>
<div class="note">ZLOG8 抬到 50% IRE 是把码值从中暗部挪给中高调的直接后果；紫色 N-Log 因载体是 10-bit，
可把灰卡压在 36% 而仍保住 104 code/档。两者形状同族，差别全部来自位深预算。</div>
</div>
<div class="card"><b>定义式</b>
<p style="margin:8px 0">正向 <code>x = 16337.2 · X^2.1030 + 16</code>（X ≤ 0.05）　<code>x = 63.0898 · ln(X) + 235</code>（X &gt; 0.05）</p>
<p style="margin:8px 0">逆向 <code>X = ((x−16)/16337.2)^(1/2.1030)</code>（x ≤ 46）　<code>X = exp((x−235)/63.0898)</code>（x &gt; 46）</p>
<div class="note">X = 场景线性亮度 / 视频满量程。趾部指数由 C¹ 连续条件唯一解出 <code>P_TOE = LN_K/(C_TOE−C_BLACK)</code>，
交界左右导数同为 1261.7950，无折角。</div></div>
<div class="card"><b>量化预算</b><table>
<tr><th>区段</th><th>X</th><th>档数</th><th>code</th><th>code/档</th></tr>
<tr><td>趾部（压缩）</td><td class="n">0 → 0.05</td><td class="n">≈5.0</td><td class="n">30</td><td class="n">非均匀</td></tr>
<tr><td>对数主段</td><td class="n">0.05 → 1.0</td><td class="n">4.32</td><td class="n">189</td><td class="n">43.73</td></tr>
<tr><td>合计</td><td class="n">0.0015 → 1.0</td><td class="n">9.4</td><td class="n">219</td><td class="n">—</td></tr></table>
<div class="note warn">硬边界：Z6 II 机内只有 H.264 8-bit（固件内 HEVC / 10-bit / Main10 零命中，无 Z8 的
eDSID_Setting_H265Mov10bitToneMode 框架），且链路 X 上限为 1.0 —— 不存在高光 headroom。
真 10-bit N-Log 无法在此机型实现，ZLOG8 是 8-bit 载体下的最优对数分配。</div></div>
<div class="card"><b>机内落地</b><table>
<tr><th>路径</th><th>载体</th><th>风险</th><th>状态</th></tr>
<tr><td>(a) NP3 卡载</td><td>256 点自定义 tone curve（tag 0x2 / len 0x242 / +0x40 起 BE16）</td><td>不改固件</td><td>曲线已就绪，BE16 值域待一个实机导出的 NP3 定标</td></tr>
<tr><td>(b) 固件数据槽</td><td>内置 gamma/tone 表替换 + K8 重签</td><td>需刷机</td><td>K8 已验证，封包可全链路自洽</td></tr></table>
<div class="note">两条路共用 <code>out/zlog8_curve_256.txt</code>（encode(bt709_eotf(in))）与 <code>out/zlog8_to_linear_33.cube</code>。
刷机未经明确同意不执行。</div></div>
</div>"""
p=os.path.join(OUT,"zlog8_curve.html"); open(p,"w",encoding="utf-8").write(html)
print(p, len(html))
