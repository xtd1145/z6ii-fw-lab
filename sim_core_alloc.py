#!/usr/bin/env python3
"""R1-R4 core-rebalance pipeline simulator (offline, read-only; firmware untouched).

Stages per frame
  sensor line -> CPU0 fg dispatch (capture SM, encoder kick) -> HW encoder
  -> post/wrap core -> IPCU mailbox -> card queue

Modelling rules learned the hard way (each one was a bug in an earlier pass)
  * Bandwidth (DDR) is charged OFF the critical path: rate constraint + a fixed
    mb/cap delay.  Putting it on-path as a FIFO made a 24 %-loaded bus invent a
    phantom 0.5 ms/frame drift.
  * A core is NOT one FIFO server.  Foreground dispatch (must finish before the
    next shutter) and background post/wrap (low priority, preemptible) are
    separate servers; total busy must stay <= 100 %.
  * The producer is pool-gated predictively: a shutter may only fire if, when its
    payload would land in the card queue, the outstanding bytes fit the frame
    pool.  Gives real burst depth and real throttling instead of pinned fps.
  * The measurement window ends when the card took the last byte, so card
    utilisation can never exceed the link.

Levers (static evidence: core_alloc.py, ipcu_probe.py, ipcu_dump.py)
  R1 SD0_/SD1_ capture state machine -> CPU1        (sdsm_on1)
  R2 IPCU_0..15 doorbells, 1 -> 4 channels in use   (leases)
  R3 shutter pulse -> ARM926 hardware timer         (section [6])
  R4 SMP-call enc-driver + post/wrap -> CPU1        (encdrv_on1, post_on1)
"""
import math
import random

GEOM = {"FX": 6048 * 4024, "DX": 3936 * 2624, "HD": 1920 * 1080,
        "UHD": 3840 * 2160}
MODES = {                        # (geom, fps, per-frame output MB)
    "FX 14 RAW":   ("FX", 14, 42.0),
    "DX 30 RAW":   ("DX", 30, 13.0),
    "DX 60 JPG":   ("DX", 60, 2.6),
    "HD 120 MOV":  ("HD", 120, 0.6),
    "UHD 30 MOV":  ("UHD", 30, 12.0),
}
LINE_MPS = GEOM["FX"] * 14 / 1e6            # sensor line, from the factory FX14 point

P = dict(
    cpu0_capture_us=250.0,     # frame descriptor / buffer hand-off / metadata
    cpu0_af_us=180.0,          # AF stats consume + lens step (stays on CPU0)
    cpu0_ui_us=120.0,          # EVF/LCD + key scan tick
    sdsm_us=320.0,             # SD0_/SD1_ capture state machine, per frame
    enc_drv_us=120.0,          # HW encoder kick + DMA descriptor fill
    post_us=550.0,             # NEF header / preview / thumbnail / HEIF wrap
    dma_us=90.0,               # per DMA chunk
    per_mb_us=9.0,             # per output MB shuffle / checksum
    chunk_mb=4.0,
    enc_mps=400.0,             # HW JPEG encoder pixel throughput
    ddr_mbps=4800.0,
    card_mbps=230.0,           # UHS-II practical
    pool_mb=600.0,             # DDR frame pool ("burst memory")
    lease_us=25.0,
    leases=1,
)

FEATURES = {                   # per-frame CPU work a mod would add, us
    "EIS warp 4K":            (4000.0, "fg"),
    "HEIF/HEVC mux":          (3000.0, "bg"),
    "focus-stack merge":      (12000.0, "bg"),
    "pixel-shift composite":  (15000.0, "bg"),
    "in-camera ND / LS sim":  (2000.0, "fg"),
    "dual-pd AF full read":   (1500.0, "fg"),
}


class Server:
    """FIFO single server: one priority class of one core, or the encoder pipe."""

    def __init__(self, name):
        self.name = name
        self.free = 0.0
        self.busy = 0.0
        self.n = 0
        self.waits = []

    def take(self, now, us):
        start = max(now, self.free)
        self.waits.append(start - now)
        self.free = start + us
        self.busy += us
        self.n += 1
        return self.free


class Bw:
    """Rate constraint. charge() off-path, enqueue() on-path (card queue)."""

    def __init__(self, name, cap):
        self.name = name
        self.cap = cap
        self.free = 0.0
        self.used = 0.0
        self.busy = 0.0
        self.n = 0
        self.waits = []

    def charge(self, now, mb):
        start = max(now, self.free)
        self.waits.append(start - now)
        us = mb / self.cap * 1e6
        self.free = start + us
        self.used += mb
        self.busy += us
        self.n += 1
        return us                                 # service delay, off-path

    def enqueue(self, now, mb):
        start = max(now, self.free)
        self.waits.append(start - now)
        self.free = start + mb / self.cap * 1e6
        self.used += mb
        self.busy += mb / self.cap * 1e6
        self.n += 1
        return self.free


class Lease:
    """IPCU doorbell channels.

    Channel occupancy is accounted and each crossing adds one fixed doorbell
    latency, but the channel is never fed back as a queue: stages of consecutive
    frames interleave in causal time, so an on-path FIFO channel invents
    head-of-line delay that the hardware does not have.  The real cost of a single
    channel is that concurrent requests cannot all be in flight, so the worst-case
    block is hold_us x (requests_per_frame - 1).
    """

    def __init__(self, n, hold_us):
        self.n = n
        self.hold = hold_us
        self.busy = 0.0
        self.reqs = 0

    def acquire(self, now):
        self.reqs += 1
        self.busy += self.hold
        return now + self.hold

    def hol_us(self, per_frame):
        return 0.0 if self.n >= per_frame else self.hold * max(0, per_frame - 1)

    def outstanding(self, now):
        return 0 if self.busy >= 0 else 1


def jz(rng):
    """Per-frame workload jitter, mean 1.0 (UI / AF / lens / SDC noise)."""
    return rng.uniform(0.8, 1.2)


def base_cfg(**kw):
    c = dict(P)
    c["sdsm_on1"] = c["encdrv_on1"] = c["post_on1"] = False
    c["feature_fg_us"] = c["feature_bg_us"] = 0.0
    c.update(kw)
    return c


VARIANTS = {
    "BASE":          lambda c: None,
    "R1":            lambda c: c.update(sdsm_on1=True),
    "R4":            lambda c: c.update(encdrv_on1=True, post_on1=True),
    "R2":            lambda c: c.update(leases=4),
    "R1+R4":         lambda c: c.update(sdsm_on1=True, encdrv_on1=True, post_on1=True),
    "OPT(R1+R2+R4)": lambda c: c.update(sdsm_on1=True, encdrv_on1=True,
                                        post_on1=True, leases=4),
}
CPU_KEYS = ("cpu0_capture_us", "cpu0_af_us", "cpu0_ui_us", "sdsm_us",
            "enc_drv_us", "post_us", "dma_us", "per_mb_us")


def pct(xs, q):
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(q * len(ys)))]


def run(mode, cfg, seed=1, sim_s=2.0):
    rec = MODES[mode]
    geom, fps, out_mb = rec[:3]
    # a binned readout can feed a smaller encoder payload; 4th field = encoder area
    enc_geom = rec[3] if len(rec) > 3 else geom
    px = GEOM[geom]
    period = 1e6 / fps
    frame_mb = px * 2 / 1e6
    readout_us = px / 1e6 / LINE_MPS * 1e6
    enc_us = GEOM[enc_geom] / 1e6 / cfg["enc_mps"] * 1e6
    target = int(round(fps * sim_s))

    r = dict(mode=mode, seed=seed, fps=fps, target=target, period=period,
             frame_mb=frame_mb, readout_us=readout_us, enc_us=enc_us,
             ddr_floor=(2 * frame_mb + 2 * out_mb) * fps, card_need=out_mb * fps)

    fg0, fg1 = Server("CPU0-fg"), Server("CPU1-fg")
    bg0, bg1 = Server("CPU0-bg"), Server("CPU1-bg")
    enc = Server("ENC")
    ddr = Bw("DDR", cfg["ddr_mbps"])
    card = Bw("CARD", cfg["card_mbps"])
    mbox = Lease(cfg["leases"], cfg["lease_us"])
    chunks = max(1, int(math.ceil(out_mb / cfg["chunk_mb"])))
    post_work = cfg["post_us"] + chunks * cfg["dma_us"] + out_mb * cfg["per_mb_us"]

    def zero(extra):
        r.update(over_line=extra, done=0, stalls=target, burst=0, eff_fps=0.0,
                 sustained_fps=0.0, shutter_fps=0.0, pool_peak_mb=0.0, p50=0.0,
                 p99=0.0, lat=[], arr=[], pre_shutter_us=0.0, drain_ms=0.0,
                 fg0=fg0, fg1=fg1, bg0=bg0, bg1=bg1, enc=enc, ddr=ddr, card=card,
                 mbox=mbox, cpu0_util=0.0, cpu1_util=0.0, enc_util=0.0,
                 cpu0_cap=0.0, cpu1_cap=0.0, enc_cap=0.0,
                 card_util=0.0, ddr_util=0.0, sim_s=sim_s, mbox_wait=0.0,
                 mbox_hol_us=0.0, mbox_load=0.0,
                 ratio=readout_us / period)
        return r

    if readout_us > period + 1e-9:                   # hard sensor-line gate
        return zero(True)

    rng = random.Random(seed * 7919 + sum(map(ord, mode)))
    lat, arrs, pending = [], [], []                  # pending = (card_done, mb)
    stalls = burst = 0
    pool_peak = 0.0
    clock = 0.0
    jit = 0.02 * period
    pre_us = readout_us + enc_us + 4e3               # shutter -> card queue
    # CPU capacity floor: a core absorbs 1 core-us per wall-us, so the shutter
    # cannot fire faster than the per-frame work loaded on one core.  These sums
    # must mirror exactly the take() calls below.
    bg_work = post_work + cfg["feature_bg_us"]
    fg0_work = cfg["cpu0_capture_us"] + cfg["cpu0_af_us"] + cfg["cpu0_ui_us"] \
        + cfg["feature_fg_us"]
    w0 = fg0_work + (0.0 if cfg["sdsm_on1"] else cfg["sdsm_us"]) \
        + (0.0 if cfg["encdrv_on1"] else cfg["enc_drv_us"]) \
        + (0.0 if cfg["post_on1"] else bg_work)
    w1 = (cfg["sdsm_us"] if cfg["sdsm_on1"] else 0.0) \
        + (cfg["enc_drv_us"] if cfg["encdrv_on1"] else 0.0) \
        + (bg_work if cfg["post_on1"] else 0.0)
    min_gap = max(w0, w1)
    eff_period = max(period, min_gap)
    xfer = 1 + cfg["sdsm_on1"] + cfg["encdrv_on1"] + cfg["post_on1"]

    for i in range(target):
        nominal = i * period
        arr = max(clock, nominal)

        # ---- predictive pool gate ----------------------------------------------
        for _ in range(target + 2):
            ready = arr + pre_us
            occ = out_mb + sum(mb for d, mb in pending if d > ready)
            if occ <= cfg["pool_mb"]:
                break
            arr = max(arr, min(d for d, _ in pending if d > ready))
        pool_peak = max(pool_peak, out_mb + sum(mb for d, mb in pending
                                                if d > arr + pre_us))
        clock = arr + eff_period
        late = arr > nominal + jit
        if late:
            stalls += 1
        elif stalls == 0:
            burst += 1
        arrs.append(arr)

        # ---- sensor line + raw write (off-path bandwidth) ----------------------
        t = arr + readout_us + ddr.charge(arr + readout_us, frame_mb)
        t = mbox.acquire(t)                          # DMA-completion ack

        # ---- foreground: must complete inside this frame -----------------------
        t = fg0.take(t, cfg["cpu0_capture_us"] * jz(rng))
        fg0.take(t, (cfg["cpu0_af_us"] + cfg["cpu0_ui_us"]) * jz(rng))
        if cfg["sdsm_on1"]:
            t = mbox.acquire(t)
        t = (fg1 if cfg["sdsm_on1"] else fg0).take(t, cfg["sdsm_us"] * jz(rng))
        if cfg["encdrv_on1"]:
            t = mbox.acquire(t)
        t = (fg1 if cfg["encdrv_on1"] else fg0).take(t, cfg["enc_drv_us"] * jz(rng))
        if cfg["feature_fg_us"]:
            t = fg0.take(t, cfg["feature_fg_us"])

        # ---- HW encode, DDR re-read + compressed write -------------------------
        t = enc.take(t, enc_us)
        t = t + ddr.charge(t, frame_mb + out_mb)

        # ---- background post/wrap (preemptible, not on the next shutter) ------
        if cfg["post_on1"]:
            t = mbox.acquire(t)
        tb = (bg1 if cfg["post_on1"] else bg0).take(t, post_work * jz(rng))
        if cfg["feature_bg_us"]:
            tb = (bg1 if cfg["post_on1"] else bg0).take(tb, cfg["feature_bg_us"])
        ddr.charge(tb, out_mb)                       # card engine reads the payload

        # ---- card queue (deliberately on-path: this IS the queue) -------------
        done = card.enqueue(tb, out_mb)
        lat.append(done - arr)
        pending.append((done, out_mb))

    arr_span = max(arrs[-1] + eff_period - arrs[0], eff_period)
    span = max(arr_span, card.free, 1.0)
    shutter_fps = target / (arr_span / 1e6)
    pre = (cfg["cpu0_capture_us"] + (0 if cfg["sdsm_on1"] else cfg["sdsm_us"])
           + (0 if cfg["encdrv_on1"] else cfg["enc_drv_us"]))
    r.update(over_line=False, done=target, stalls=stalls, burst=burst,
             eff_fps=target / (span / 1e6), sustained_fps=min(shutter_fps, fps),
             shutter_fps=shutter_fps, drain_ms=(card.free - arrs[-1]) / 1e3,
             pool_peak_mb=pool_peak, p50=pct(lat, .50), p99=pct(lat, .99),
             lat=lat, arr=arrs, pre_shutter_us=pre,
             mbox_hol_us=mbox.hol_us(xfer),
             mbox_load=mbox.busy / max(arr_span, 1.0),
             fg0=fg0, fg1=fg1, bg0=bg0, bg1=bg1, enc=enc, ddr=ddr, card=card,
             mbox=mbox, sim_s=span / 1e6)
    c0 = (fg0.busy + bg0.busy) / span
    c1 = (fg1.busy + bg1.busy) / span
    r.update(cpu0_util=c0, cpu1_util=c1, enc_util=enc.busy / span,
             card_util=card.busy / span, ddr_util=ddr.busy / span,
             cpu0_cap=(fg0.busy + bg0.busy) / arr_span,
             cpu1_cap=(fg1.busy + bg1.busy) / arr_span,
             enc_cap=enc.busy / arr_span)
    return r


def check(r, cfg):
    """Invariant battery -> list of violations (empty == clean)."""
    bad = []
    if r["over_line"]:
        return ["OVER-line mode produced frames"] if r["done"] else bad
    if r["done"] != r["target"]:
        bad.append("frame conservation %d != %d" % (r["done"], r["target"]))
    if min(r["fg0"].busy, r["fg1"].busy, r["ddr"].used, r["card"].used) < 0:
        bad.append("negative counter")
    if any(b < a for a, b in zip(r["arr"], r["arr"][1:])):
        bad.append("shutter clock went backwards")
    if r["shutter_fps"] > r["fps"] + 1e-6:
        bad.append("shutter fps %.4f > target %d" % (r["shutter_fps"], r["fps"]))
    if r["eff_fps"] > r["shutter_fps"] + 1e-6:
        bad.append("avg above shutter rate")
    for name, val in (("CPU0", r["cpu0_util"]), ("CPU1", r["cpu1_util"]),
                      ("ENC", r["enc_util"])):
        if val > 1.001 and r["stalls"] == 0:
            bad.append("%s oversubscribed (%.3f) with 0 stalls" % (name, val))
        if val > 1.35:
            bad.append("%s utilisation %.3f above the work floor" % (name, val))
    for name in ("ddr", "card"):
        if r[name + "_util"] > 1.0 + 1e-6:
            bad.append("%s over-delivered %.4f" % (name, r[name + "_util"]))
    if r["pool_peak_mb"] > cfg["pool_mb"] + 1e-6:
        bad.append("pool overflow %.1f > %.1f MB" % (r["pool_peak_mb"], cfg["pool_mb"]))
    if r["mbox"].outstanding(1e12):
        bad.append("lease never released")
    xfer = 1 + cfg["sdsm_on1"] + cfg["encdrv_on1"] + cfg["post_on1"]
    if r["mbox"].reqs != xfer * r["target"] or r["enc"].n != r["target"]:
        bad.append("call count lease=%d/%d enc=%d tgt=%d"
                   % (r["mbox"].reqs, xfer * r["target"], r["enc"].n, r["target"]))
    ncpu = r["fg0"].n + r["fg1"].n + r["bg0"].n + r["bg1"].n
    exp = 5 * r["target"] + (1 if cfg["feature_fg_us"] else 0) \
        + (1 if cfg["feature_bg_us"] else 0)
    if ncpu != exp:
        bad.append("CPU task count %d != %d" % (ncpu, exp))
    if r["p99"] + 1e-9 < r["p50"]:
        bad.append("p99 < p50")
    if min(r["lat"]) < 0 or not 0 <= r["burst"] <= r["target"]:
        bad.append("negative latency or burst out of range")
    want = r["target"] * MODES[r["mode"]][2]
    if abs(r["card"].used - want) > 1e-6:
        bad.append("card bytes %.1f != %.1f" % (r["card"].used, want))
    return bad


def attrib(r):
    """Per-stage mean queue growth, us per frame -> who is binding."""
    stages = {"CPU0fg": r["fg0"].waits, "CPU1fg": r["fg1"].waits,
              "CPU0bg": r["bg0"].waits, "CPU1bg": r["bg1"].waits,
              "ENC": r["enc"].waits, "DDR": r["ddr"].waits, "CARD": r["card"].waits}
    out = {}
    for k, v in stages.items():
        n = len(v)
        if n >= 9:
            k3 = n // 3
            out[k] = (sum(v[-k3:]) / k3 - sum(v[:k3]) / k3) / (n - 1)
        else:
            out[k] = 0.0
    return out


def main():
    print("EXPEED dual-CA7 rebalance simulator v2  (read-only offline model)")
    print("=" * 92)
    print("[1] floors derived from the single factory-measured point (FX 14 RAW)")
    print("    sensor line = %.2f MP/s = %.0f MB/s raw @16bit" % (LINE_MPS, LINE_MPS * 2))
    print("    %-11s %-10s %-10s %6s %-12s %s"
          % ("mode", "readout", "period", "ratio", "DDR floor", "card demand"))
    for mode in MODES:
        geom, fps, out_mb = MODES[mode]
        px = GEOM[geom]
        ro = px / 1e6 / LINE_MPS * 1e6
        per = 1e6 / fps
        floor = (2 * px * 2 / 1e6 + 2 * out_mb) * fps
        print("    %-11s %8.1fus %8.1fus %6.2f %7.0f MB/s %6.0f MB/s %s"
              % (mode, ro, per, ro / per, floor, out_mb * fps,
                 "<- LINE-BOUND %.2fx" % (ro / per) if ro > per else ""))

    seeds = list(range(1, 6))
    print("\n" + "=" * 92)
    print("[2] BASE / single-lever / OPT -- mean of 5 seeds (pool %.0f MB, card %.0f MB/s)"
          % (P["pool_mb"], P["card_mbps"]))
    print("    %-15s %-11s %6s %8s %8s %8s %8s %7s %7s %6s %5s  %s"
          % ("variant", "mode", "burst", "shutFPS", "avgFPS", "drain s", "p50ms",
             "CPU0%", "CPU1%", "fgus", "viol", "binding"))
    for vname, mutate in VARIANTS.items():
        for mode in MODES:
            keys = ("burst", "shutter_fps", "eff_fps", "drain_ms", "p50",
                    "cpu0_cap", "cpu1_cap", "pre_shutter_us")
            agg = {k: [] for k in keys}
            aagg = {}
            viol = 0
            for s in seeds:
                cfg = base_cfg()
                mutate(cfg)
                r = run(mode, cfg, s)
                viol += len(check(r, cfg))
                for k in keys:
                    agg[k].append(r[k])
                for k, v in attrib(r).items():
                    aagg.setdefault(k, []).append(v)
            m = lambda k: sum(agg[k]) / len(agg[k])
            a = {k: sum(v) / len(v) for k, v in aagg.items()}
            worst = max(a, key=lambda k: a[k])
            bind = ("none" if a[worst] < 50 else "%s %+0.0fus/f" % (worst, a[worst]))
            print("    %-15s %-11s %6.1f %8.2f %8.2f %8.1f %8.1f %7.1f %7.1f %6.0f %5d  %s"
                  % (vname, mode, m("burst"), m("shutter_fps"), m("eff_fps"),
                     m("drain_ms") / 1e3, m("p50") / 1e3, m("cpu0_cap") * 100,
                     m("cpu1_cap") * 100, m("pre_shutter_us"), viol, bind))

    print("\n" + "=" * 92)
    print("[3] invariant battery: %d variants x %d modes x %d seeds = %d runs"
          % (len(VARIANTS), len(MODES), len(seeds),
             len(VARIANTS) * len(MODES) * len(seeds)))
    total = fails = 0
    for vname in VARIANTS:
        for mode in MODES:
            msgs = []
            for s in seeds:
                cfg = base_cfg()
                VARIANTS[vname](cfg)
                msgs += check(run(mode, cfg, s), cfg)
            total += len(msgs)
            fails += 1 if msgs else 0
            if msgs:
                print("    %-18s %-11s FAIL: %s"
                      % (vname, mode, "; ".join(msgs[:3])))
    print("    -> %d runs, %d failing, %d violation messages"
          % (len(VARIANTS) * len(MODES) * len(seeds), fails, total))

    print("\n" + "=" * 92)
    print("[4] CPU pixel-rate ceiling: where the core, not the line, would bind")
    for vname in ("BASE", "OPT(R1+R2+R4)"):
        cfg = base_cfg()
        VARIANTS[vname](cfg)
        per_fx = (cfg["cpu0_capture_us"] + cfg["cpu0_af_us"] + cfg["cpu0_ui_us"]
                  + cfg["sdsm_us"] + cfg["enc_drv_us"] + cfg["post_us"]
                  + 11 * cfg["dma_us"] + 42 * cfg["per_mb_us"])
        mp = GEOM["FX"] / 1e6
        us_per_mp = per_fx / mp
        # one core at 100 %; OPT splits fg/bg across two
        ceil1 = 1e6 / us_per_mp
        print("    %-15s CPU work %.3f us/MP  -> single-core ceiling %6.1f MP/s "
              "(line = %.1f MP/s, %.1fx head-room)"
              % (vname, us_per_mp, ceil1, LINE_MPS, ceil1 / LINE_MPS))

    print("\n" + "=" * 92)
    print("[5] CPU speed sensitivity (task times x 1/scale)")
    print("    %-11s %-7s %-15s %8s %8s %8s %8s %8s"
          % ("mode", "scale", "variant", "shutFPS", "avgFPS", "burst", "CPU0%", "CPU1%"))
    for mode in ("HD 120 MOV", "DX 30 RAW", "FX 14 RAW"):
        for scale in (0.1, 0.2, 0.5, 1):
            for vname in ("BASE", "OPT(R1+R2+R4)"):
                cfg = base_cfg()
                VARIANTS[vname](cfg)
                for k in CPU_KEYS:
                    cfg[k] = cfg[k] / scale
                r = run(mode, cfg, 1)
                print("    %-11s x%-6.1f %-15s %8.2f %8.2f %8.1f %8.1f %8.1f"
                      % (mode, scale, vname, r["shutter_fps"], r["eff_fps"],
                         r["burst"], r["cpu0_cap"] * 100, r["cpu1_cap"] * 100))

    print("\n" + "=" * 92)
    print("[6] card x pool sensitivity (OPT, seed 1) -- the stills bottleneck")
    print("    %-11s %7s %8s %9s %9s %9s %9s"
          % ("mode", "card", "pool", "shutFPS", "avgFPS", "burst", "drain s"))
    for mode in ("FX 14 RAW", "DX 30 RAW"):
        for card in (180, 230, 300, 600):
            for pool in (150, 600, 1200):
                cfg = base_cfg(card_mbps=card, pool_mb=pool)
                VARIANTS["OPT(R1+R2+R4)"](cfg)
                r = run(mode, cfg, 1)
                print("    %-11s %7d %8.0f %9.2f %9.2f %9.1f %9.1f"
                      % (mode, card, pool, r["shutter_fps"], r["eff_fps"],
                         r["burst"], r["drain_ms"] / 1e3))

    print("\n" + "=" * 92)
    print("[7] feature head-room: cumulative wishlist load, BASE vs OPT, seed 1")
    STILLS_FG = ["dual-pd AF full read", "in-camera ND / LS sim"]
    STILLS_BG = ["focus-stack merge", "pixel-shift composite", "HEIF/HEVC mux"]
    VIDEO_FG = ["EIS warp 4K", "dual-pd AF full read", "in-camera ND / LS sim"]
    VIDEO_BG = ["HEIF/HEVC mux"]
    scen = []
    for tag, fg_list, bg_list, mode in (
            ("FX14 stills", STILLS_FG, STILLS_BG, "FX 14 RAW"),
            ("UHD30 video", VIDEO_FG, VIDEO_BG, "UHD 30 MOV"),
            ("HD120 video", VIDEO_FG, VIDEO_BG, "HD 120 MOV")):
        fg_us = sum(FEATURES[f][0] for f in fg_list if FEATURES[f][1] == "fg")
        bg_us = sum(FEATURES[f][0] for f in bg_list if FEATURES[f][1] == "bg")
        bg_us += sum(FEATURES[f][0] for f in fg_list if FEATURES[f][1] == "bg")
        fg_us += sum(FEATURES[f][0] for f in bg_list if FEATURES[f][1] == "fg")
        scen.append((tag, mode, fg_us, bg_us, fg_list + bg_list))
    print("    %-14s %-12s %8s %8s | %-30s | %-30s"
          % ("scenario", "mode", "fg us/f", "bg us/f", "BASE", "OPT"))
    for tag, mode, fg_us, bg_us, items in scen:
        cells = []
        for vname in ("BASE", "OPT(R1+R2+R4)"):
            cfg = base_cfg(feature_fg_us=fg_us, feature_bg_us=bg_us)
            VARIANTS[vname](cfg)
            r = run(mode, cfg, 1, sim_s=1.0)
            peak = max(r["cpu0_cap"], r["cpu1_cap"])
            cells.append("c0=%5.1f%% c1=%5.1f%% shut=%6.2f %s"
                         % (r["cpu0_cap"] * 100, r["cpu1_cap"] * 100,
                            r["shutter_fps"],
                            "FITS" if r["stalls"] == 0 and peak < 0.95
                            else "OVER(%d stalls)" % r["stalls"]))
        print("    %-14s %-12s %8.0f %8.0f | %-30s | %-30s"
              % (tag, mode, fg_us, bg_us, cells[0], cells[1]))
    print("    items: FX14 = %s" % ", ".join(STILLS_FG + STILLS_BG))
    print("           video = %s" % ", ".join(VIDEO_FG + VIDEO_BG))

    print("\n" + "=" * 92)
    print("[8] shutter timing budget for 1/20000 (enum code 35, already in flash)")
    print("    4 ladder steps per octave -> rung spacing 0.25 EV, so the rung is not")
    print("    the limit; a 1/3 EV gate error allows e*(1-2^-1/3) = 0.206*e.")
    print("    %-10s %-7s %10s %14s" % ("shutter", "code", "gate us", "max error us"))
    for code, den in ((24, 4000), (30, 8000), (34, 16000), (35, 20000), (39, 40000)):
        e = 1e6 / den
        print("    1/%-9d %-7d %10.2f %14.2f" % (den, code, e, e * 0.2063))
    print("    commit chain that must finish before the pulse:")
    for vname in ("BASE", "R1", "R4", "OPT(R1+R2+R4)"):
        cfg = base_cfg()
        VARIANTS[vname](cfg)
        r = run("FX 14 RAW", cfg, 1)
        print("      %-16s CPU0 fg commit %5.0f us   p99 CPU0 fg wait %7.1f us"
              % (vname, r["pre_shutter_us"], pct(r["fg0"].waits, 0.99)))
    print("      R3 (ARM926 dual-input timer owns the pulse): <= 2 us, so 1/20000")
    print("      (10.3 us budget) and even 1/40000 (5.2 us) fit; a CPU0-resident")
    print("      pulse cannot bound its own commit chain, which is why the factory")
    print("      caps the ladder by a policy constant, not by a missing enum entry.")
    print("    light cost, EV = log2(N^2/t), Z6 II base ISO 100")
    print("    %-9s %s" % ("shutter", "".join("  f/%-4.1f:    EV     ISO" % a
                                              for a in (1.8, 2.8, 4.0))))
    for d in (8000, 16000, 20000, 40000):
        cells = []
        for a in (1.8, 2.8, 4.0):
            ev = math.log2(a * a * d)
            iso = a * a * d / 2 ** 15 * 100          # scene EV15 = full sun
            cells.append("  %6.2f %7.0f" % (ev, iso))
        print("    1/%-8d %s" % (d, "".join(cells)))
    print("    EV15 = full sun, EV16.5 = snow/sand; ISO < 100 -> over-exposed, so")
    print("    1/20000 wants f/1.4-f/2.8 in full sun or an ND filter.")

    print("\n" + "=" * 92)
    print("[9] lever -> sim verdict -> blocker")
    print("    R1 SD0_/SD1_ SM -> CPU1   : CPU0 commit %d -> %d us, stills fps unchanged"
          % (int(P["cpu0_capture_us"] + P["sdsm_us"] + P["enc_drv_us"]),
             int(P["cpu0_capture_us"])))
    m1 = run("FX 14 RAW", base_cfg(sdsm_on1=True, encdrv_on1=True, post_on1=True,
                                  leases=1), 1)
    m4 = run("FX 14 RAW", base_cfg(sdsm_on1=True, encdrv_on1=True, post_on1=True,
                                   leases=4), 1)
    print("    R2 IPCU channels 1 -> 4   : worst-case doorbell block %.0f -> %.0f us"
          % (m1["mbox_hol_us"], m4["mbox_hol_us"]))
    print("                                channel load %.2f%% (4 xfers x 25 us)"
          % (m4["mbox_load"] * 100))
    print("                                (only matters once R1/R4 migrate work)")
    print("    R3 shutter -> ARM926 HW   : unlocks code 35 = 1/20000 (see [8])")
    print("    R4 enc-driver+post -> CPU1: CPU0 load %.1f%% -> %.1f%% at HD120"
          % (run("HD 120 MOV", base_cfg(), 1)["cpu0_util"] * 100,
             run("HD 120 MOV", base_cfg(post_on1=True, encdrv_on1=True), 1)["cpu0_util"] * 100))
    print("    stills fps is gated by card + pool, not by either core (see [6])")
    print("    DX60 is gated by the sensor line at %.2fx, no core allocation helps"
          % (GEOM["DX"] / 1e6 / LINE_MPS * 1e6 / (1e6 / 60)))
    print("    blockers: eg1850_sla 67 KB head-room, module CRC, package CRC,")
    print("              SHA1(body||K8) -> K8 unknown -> cannot re-sign")


if __name__ == "__main__":
    main()
