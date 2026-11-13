#!/usr/bin/env python3
"""DX30 RAW / DX45 JPEG budget under two readout models. Read-only; writes only this dir.

Input constants: source and confidence
--------------------------------------
LINE12  Nikon Z6 II spec: Continuous H (extended) = approx 14 fps; FX 24.337MP * 14.
        Factory point, also used by sim_core_alloc.py. Confidence: high.
LINE14  Nikon Z6 II spec: 14-bit NEF/RAW = approx 10 fps; FX * 10.
        Confidence: high for FX; medium when the same 1.4x cost is extrapolated to DX.
ENC     JPEG encoder throughput = 400 MP/s. NOT measured. The only hard data point is
        the shipped FX 14fps JPEG mode (340.7 MP/s). Confidence: low-medium.
POOL    Frame buffer pool = 600 MB. NOT measured. Confidence: low.
CARDS   SD UHS-II 230 / CFe Type B 400 / CFe B top 700 MB/s. Typical card-class specs,
        NOT measured on this body. Confidence: medium.
PAY     Median MB/frame for each format and size. Estimated from resolution, bit depth
        and typical compression ratios; NOT measured from files. Confidence: low-medium.
        See REPORT.md section 2.1 for the derivation and sensitivity.
"""
import json, os

PX = {"DXL": 3936*2624, "DXM": 2944*1968, "DXS": 1968*1312,
      "DXBIN": 1968*1312, "FX": 6048*4024}
LINE12 = PX["FX"] * 14 / 1e6      # 340.72 MP/s; source: factory 14fps 12-bit point; high confidence
LINE14 = PX["FX"] * 10 / 1e6      # 243.37 MP/s; source: Nikon 14-bit ~10fps; high FX / medium DX
ENC = 400.0                       # assumption, not measured; low-medium confidence
POOL = 600.0                      # assumption, not measured; low confidence
CARDS = {"SD UHS-II": 230.0, "CFe Type B": 400.0, "CFe B top": 700.0}  # class specs, not measured

# MB/frame median payloads (12-bit lossless / 14-bit uncompressed / JPEG Fine)
# Estimated, not measured: resolution x bit depth x typical compression ratio.
PAY = {
 ("DXL","RAW lossless"):11.5, ("DXL","RAW 14bit"):20.7, ("DXL","RAW hi-eff"):8.0,
 ("DXM","RAW lossless"):6.4,  ("DXM","RAW 14bit"):11.6, ("DXM","RAW hi-eff"):4.5,
 ("DXS","RAW lossless"):2.9,  ("DXS","RAW 14bit"):5.2,  ("DXS","RAW hi-eff"):2.0,
 ("DXL","JPG Fine"):7.0, ("DXM","JPG Fine"):4.0, ("DXS","JPG Fine"):1.9,
}

def row(name, readout_px, out_key, fps, fmt, bit):
    mps = readout_px/1e6*fps
    line = LINE14 if bit == 14 else LINE12
    mb = PAY[(out_key,fmt)]
    need = mb*fps
    frames = int(POOL//mb)
    fits = [k for k,v in CARDS.items() if need <= v]
    verdict = []
    if mps > line: verdict.append("READOUT %.2fx" % (mps/line))
    if fmt.startswith("JPG") and mps > ENC: verdict.append("ENC %.2fx" % (mps/ENC))
    if not fits: verdict.append("NO CARD")
    if bit == 14 and fmt.startswith("RAW") and fmt != "RAW 14bit":
        pass
    return dict(name=name, readout_px=readout_px, out=out_key, fps=fps, fmt=fmt,
                bit=bit, mps=round(mps,1), line=round(line,2),
                mb_frame=mb, mb_s=round(need,1), pool_frames=frames,
                pool_seconds=round(frames/fps,2), cards=fits,
                verdict="; ".join(verdict) if verdict else "CLEAR")

rows = [
 # conservative model: all DX still sizes read the full DX area; 2x2 bin is a separate readout
 row("DX45 JPEG, full-DX readout, L output", PX["DXL"], "DXL", 45, "JPG Fine", 12),
 row("DX45 JPEG, full-DX readout, M output", PX["DXL"], "DXM", 45, "JPG Fine", 12),
 row("DX45 JPEG, full-DX readout, S output", PX["DXL"], "DXS", 45, "JPG Fine", 12),
 row("DX45 JPEG, 2x2 bin readout, 2.6MP",    PX["DXBIN"], "DXS", 45, "JPG Fine", 12),
 row("DX30 RAW, full-DX readout, 12-bit lossless", PX["DXL"], "DXL", 30, "RAW lossless", 12),
 row("DX30 RAW, full-DX readout, 12-bit hi-eff",   PX["DXL"], "DXL", 30, "RAW hi-eff", 12),
 row("DX30 RAW, full-DX readout, 14-bit",          PX["DXL"], "DXL", 30, "RAW 14bit", 14),
 row("DX30 RAW, 2x2 bin readout, 14-bit",          PX["DXBIN"], "DXS", 30, "RAW 14bit", 14),
 # 14-bit full-DX max fps
 dict(name="DX 14-bit full-DX ceiling", readout_px=PX["DXL"], out="DXL", fps=round(LINE14/(PX["DXL"]/1e6),2),
      fmt="RAW 14bit", bit=14, mps=round(LINE14,1), line=round(LINE14,2),
      mb_frame=PAY[("DXL","RAW 14bit")], mb_s=round(PAY[("DXL","RAW 14bit")]*LINE14/(PX["DXL"]/1e6),1),
      pool_frames=int(POOL//PAY[("DXL","RAW 14bit")]),
      pool_seconds=round((POOL//PAY[("DXL","RAW 14bit")])/(LINE14/(PX["DXL"]/1e6)),2),
      cards=[k for k,v in CARDS.items() if PAY[("DXL","RAW 14bit")]*LINE14/(PX["DXL"]/1e6)<=v],
      verdict="ceiling"),
]

out = dict(px=PX, line12=round(LINE12,2), line14=round(LINE14,2), enc=ENC,
           pool=POOL, cards=CARDS, rows=rows)
here=os.path.dirname(os.path.abspath(__file__))
json.dump(out, open(os.path.join(here,"budget.json"),"w"), ensure_ascii=False, indent=2)
print("readout line: 12-bit %.2f MP/s, 14-bit %.2f MP/s (Nikon 14fps/10fps spec)" % (LINE12,LINE14))
for r in rows:
    print("%-46s %6.1f MP/s  %6.1f MB/s  pool %3d f/%4.2fs  %-18s %s" %
          (r["name"], r["mps"], r["mb_s"], r["pool_frames"], r["pool_seconds"],
           ",".join(r["cards"]) or "-", r["verdict"]))
print("\nwrote", os.path.join(here,"budget.json"))
