"""DX 45 fps JPEG / DX 30 fps RAW feasibility, using the validated rebalance engine.

Geometry ids and readout windows come from the still-size table in eg1850_mas at
file offset 0x9417B4 ({w,h,id} records, id 8 = DX L, 9 = DX M, 10 = DX S).
Payload sizes are median values for Z6 II NEF/JPEG files; the +/- column carries
the spread actually seen in published samples.
"""
import sys

sys.path.insert(0, r"D:\z6ii_fw_lab")
import sim_core_alloc as SC

# ---- readout windows (sensor area actually scanned) -------------------------
SC.GEOM.update({
    "DXL": 3936 * 2624,      # 10.328 MP  still id 8
    "DXM": 2944 * 1968,      #  5.793 MP  still id 9
    "DXS": 1968 * 1312,      #  2.582 MP  still id 10
    "DX169": 3936 * 2216,    #  8.722 MP  DX 16:9
})

LINE = SC.LINE_MPS                    # 340.72 MP/s provisioned stills line
ENC = SC.P["enc_mps"]                 # 400 MP/s JPEG/HEVC encode model
CARDS = {"SD UHS-II": 230.0, "CFe Type B": 400.0, "CFe B (top)": 700.0}

# out_mb = (median, spread) per (window, format)
PAYLOAD = {
    ("DXL", "JPG Fine"): (7.0, 1.5), ("DXL", "JPG Norm"): (4.0, 1.0),
    ("DXL", "JPG Basic"): (2.2, 0.5), ("DXL", "RAW 14bit"): (20.7, 0.2),
    ("DXL", "RAW lossless"): (11.5, 1.5), ("DXL", "RAW hi-eff"): (8.0, 1.5),
    ("DXM", "JPG Fine"): (4.0, 1.0), ("DXM", "JPG Norm"): (2.3, 0.5),
    ("DXM", "JPG Basic"): (1.3, 0.3), ("DXM", "RAW 14bit"): (11.6, 0.2),
    ("DXM", "RAW lossless"): (6.4, 0.9), ("DXM", "RAW hi-eff"): (4.5, 0.9),
    ("DXS", "JPG Fine"): (1.9, 0.4), ("DXS", "JPG Norm"): (1.1, 0.2),
    ("DXS", "JPG Basic"): (0.65, 0.15), ("DXS", "RAW 14bit"): (5.2, 0.1),
    ("DXS", "RAW lossless"): (2.9, 0.4), ("DXS", "RAW hi-eff"): (2.0, 0.4),
}
FMTS = ["JPG Fine", "JPG Norm", "JPG Basic", "RAW 14bit", "RAW lossless", "RAW hi-eff"]

print("DX 45/30 feasibility -- engine: sim_core_alloc v2 (150-run battery clean)")
print("provisioned stills line = %.2f MP/s | encoder model = %.0f MP/s | pool = %.0f MB"
      % (LINE, ENC, SC.P["pool_mb"]))

# ---- [1] static gate arithmetic --------------------------------------------
print("\n[1] per-window readout arithmetic (fps vs line rate)")
print("    %-6s %-12s %8s  %8s %8s   %s" % ("win", "MP", "fps", "MP/s", "x line", "verdict"))
for win in ("DXL", "DX169", "DXM", "DXS"):
    mp = SC.GEOM[win] / 1e6
    for fps in (30, 45, 60):
        mps = mp * fps
        ok = mps <= LINE
        print("    %-6s %-12.3f %8d  %8.1f %7.2fx   %s"
              % (win, mp, fps, mps, mps / LINE,
                 "within line" if ok else "OVER LINE %.2fx -> needs a readout mode the image never provisions" % (mps / LINE)))

# ---- [2] card + buffer gate --------------------------------------------------
print("\n[2] payload / card / burst gates")
print("    %-5s %-11s %6s %7s %9s  %-22s %8s" % ("fps", "win", "fmt", "MB/fr", "MB/s", "card verdict", "burst"))
rows = []
for fps in (30, 45):
    for win in ("DXL", "DXM", "DXS"):
        for fmt in FMTS:
            mb, sp = PAYLOAD[(win, fmt)]
            need = mb * fps
            best = max(CARDS, key=lambda k: CARDS[k])
            ok_on = [k for k, v in CARDS.items() if need <= v]
            burst = int(SC.P["pool_mb"] // mb)
            rows.append((fps, win, fmt, mb, need, ok_on, burst))
            print("    %-5d %-11s %6s %7.2f %9.1f  %-22s %8d"
                  % (fps, win, fmt, mb, need,
                     ("fits " + ",".join(ok_on)) if ok_on else "no card fits (%0.f..%0.f)" % (min(CARDS.values()), max(CARDS.values())),
                     burst))

# ---- [3] simulate every (fps, win, fmt) with the validated engine -----------
print("\n[3] simulation, mean of 5 seeds, OPT(R1+R2+R4) allocation, CFe Type B card")
seeds = [1, 2, 3, 4, 5]
print("    %-5s %-6s %-11s %7s %8s %8s %8s %8s %7s %6s  %s"
      % ("fps", "win", "fmt", "overln", "shutFPS", "avgFPS", "p50ms", "drainms",
         "CPU0%", "viol", "binding"))
for fps in (30, 45):
    for win in ("DXL", "DXM", "DXS"):
        for fmt in FMTS:
            mb = PAYLOAD[(win, fmt)][0]
            name = "%s %d %s" % (win, fps, fmt)
            # encoder sees the output area, readout sees the scanned window
            SC.MODES[name] = (win, fps, mb, win)
            agg, aagg, viol = {}, {}, 0
            for s in seeds:
                cfg = SC.base_cfg()
                SC.VARIANTS["OPT(R1+R2+R4)"](cfg)
                cfg["card_mbps"] = CARDS["CFe Type B"]
                r = SC.run(name, cfg, s)
                viol += len(SC.check(r, cfg))
                for k in ("shutter_fps", "eff_fps", "p50", "drain_ms", "cpu0_cap"):
                    agg.setdefault(k, []).append(r[k])
                agg.setdefault("ratio", []).append(r["readout_us"] / r["period"])
                for k, v in SC.attrib(r).items():
                    aagg.setdefault(k, []).append(v)
            m = lambda k: sum(agg[k]) / len(agg[k])
            a = {k: sum(v) / len(v) for k, v in aagg.items()}
            worst = max(a, key=lambda k: a[k])
            bind = "none" if a[worst] < 50 else "%s %+0.0fus/f" % (worst, a[worst])
            print("    %-5d %-6s %-11s %7.2f %8.2f %8.2f %8.1f %8.1f %7.1f %6d  %s"
                  % (fps, win, fmt, m("ratio"), m("shutter_fps"), m("eff_fps"),
                     m("p50") / 1e3, m("drain_ms"), m("cpu0_cap") * 100, viol, bind))

# ---- [4) SD-card reality check for the two headline targets -----------------
print("\n[4] headline targets, weak-link table")
for fps, win, fmt in ((45, "DXL", "JPG Fine"), (45, "DXM", "JPG Fine"),
                      (45, "DXS", "JPG Fine"), (30, "DXL", "RAW lossless"),
                      (30, "DXL", "RAW 14bit"), (30, "DXM", "RAW lossless"),
                      (30, "DXS", "RAW lossless")):
    mb = PAYLOAD[(win, fmt)][0]
    need = mb * fps
    mps = SC.GEOM[win] / 1e6 * fps
    blockers = []
    if mps > LINE:
        blockers.append("line %.2fx" % (mps / LINE))
    if mps > ENC:
        blockers.append("enc %.2fx" % (mps / ENC))
    if need > CARDS["SD UHS-II"]:
        blockers.append("SD %.0f>230" % need)
    if int(SC.P["pool_mb"] // mb) < 20:
        blockers.append("burst %d" % int(SC.P["pool_mb"] // mb))
    print("    DX %-4s %2d fps %-12s %6.1f MP/s  %6.1f MB/s  -> %s"
          % (win, fps, fmt, mps, need, " / ".join(blockers) if blockers else "CLEAR"))

print("\n[5] firmware evidence, re-checked this pass (earlier notes corrected)")
print("    0x92CF50  video readout table, 18 records: fps in {24,25,30,50,60,100,120};")
print("              max pixel rate %.1f MP/s (UHD30) -- no stills entry"
      % (3840 * 2160 * 30 / 1e6))
print("    0x19A5A50 ordered output-size list, (w,h) u16 pairs stride 4:")
print("              3024x3024 6048x4024 4528x3016 3024x2016 6048x3400 4528x2544")
print("              3024x1696 3936x2624 2944x1968 1968x1312 3936x2214")
print("              -> DX L/M/S exist as ENCODED SIZES, not proven readout modes")
print("    0x941490  aspect/crop ladder (fixed-height rows: 4024, 3016, 2624, 1968...)")
print("    0x92F648  NOT a release-rate ladder: one 30, then even 10..62 (27 values)")
print("              then odd 9..61 (27 values).  Consumer unidentified; 45 is in the")
print("              odd run and 60 in the even run, so this proves nothing about fps.")
print("    retracted: 'no 45 fps code' -- that read was a truncated 8-column view.")
print("    absent:    preshoot symbols (PreShootingCache/Pshot/EXCP_PSHOT) appear")
print("              0 times in every Z6 II module; Z8 hits are Linux 'snapshot' substrings.")
