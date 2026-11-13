"""Read the live GIC distributor state out of a running board.

The firmware's own enable set says which interrupt lines it actually waits
on.  Comparing that set against the lines the model drives is the shortest
path to the missing "frame ready" source: any enabled INTID that never
asserts in the model is a producer we have not built.

  python gic_probe.py --at 40,60 --aux <real params> --port-base 4720
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_probe as bp

GICD = 0x04001000
ISENABLER = GICD + 0x100
ISPENDR = GICD + 0x200
ISACTIVER = GICD + 0x300
IPRIORITYR = GICD + 0x400
ITARGETSR = GICD + 0x800
ICFGR = GICD + 0xC00

NAMES = {
    45: "sequencer", 49: "engine-doorbell", 86: "sync-bank0", 87: "sync-bank1",
    88: "auto-sample", 89: "sensor", 94: "input-bank0", 95: "input-bank1",
    96: "input-bank2", 97: "input-bank3", 98: "input-bank4",
    101: "cvp", 102: "transport", 129: "graphics0", 130: "graphics1",
    131: "graphics2", 172: "delay0", 192: "spi0a", 193: "spi0b",
    214: "i2c0", 236: "timer236", 237: "timer237", 242: "timer242",
    243: "timer243", 244: "timer244", 246: "doorbell246",
}


def parse_state(text):
    """engine<N>;irq,en,pd,ac,lv,tgt,grp;...;engine<N+1>;..."""
    engines = []
    for chunk in text.strip().split("engine"):
        chunk = chunk.strip().rstrip('"')
        if not chunk:
            continue
        head, _, rest = chunk.partition(";")
        rows = []
        for item in rest.split(";"):
            parts = [p for p in item.split(",") if p]
            if len(parts) != 7:
                continue
            irq, en, pd, ac, lv, tgt, grp = (int(x) for x in parts)
            rows.append((irq, en, pd, ac, lv, tgt, grp))
        engines.append({"engine": int(head), "rows": rows})
    return engines


def snapshot(hmp, tag):
    raw = hmp.cmd("qom-get /machine gic-state")
    text = "\n".join(l for l in raw.splitlines()
                     if "engine" in l or re.match(r"^[\s\"]*[0-9]", l))
    engines = parse_state(text)
    print("== %s ==" % tag)
    for eng in engines:
        enabled = [r[0] for r in eng["rows"] if r[1]]
        pending = [r[0] for r in eng["rows"] if r[2]]
        active = [r[0] for r in eng["rows"] if r[3]]
        level = [r[0] for r in eng["rows"] if r[4]]
        print(" engine%d enabled (%d)" % (eng["engine"], len(enabled)))
        for irq, en, pd, ac, lv, tgt, grp in eng["rows"]:
            if not en:
                continue
            print("   %3d %-14s en=%d pd=%d ac=%d lv=%d tgt=%02x grp=%d" %
                  (irq, NAMES.get(irq, ""), en, pd, ac, lv, tgt, grp))
        print("   pending: %s" % " ".join(map(str, pending)))
        print("   active : %s" % " ".join(map(str, active)))
        print("   level  : %s" % " ".join(map(str, level)))
    return engines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="40")
    ap.add_argument("--aux", default="")
    ap.add_argument("--master", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--port-base", type=int, default=4720)
    ap.add_argument("--out", default="run/gic")
    ap.add_argument("--no-adc", action="store_true")
    ap.add_argument("--trace", default="",
                    help="comma separated trace events to record")
    ap.add_argument("--menu-start", type=float, default=0)
    ap.add_argument("--menu-hold", type=float, default=2.0)
    ap.add_argument("--menu-every", type=float, default=0)
    args = ap.parse_args()
    args.out = os.path.abspath(args.out)
    os.makedirs(args.out, exist_ok=True)

    cmd = [c for c in json.load(open(bp.CMD))]
    cmd[0] = bp.QEMU
    if args.master:
        for i, part in enumerate(cmd):
            if "master-boot-image=" in part:
                cmd[i] = re.sub(r"master-boot-image=[^,]*",
                                "master-boot-image=" + args.master, part)
                break
    if args.aux:
        for i, part in enumerate(cmd):
            if "master-boot-aux=" in part:
                cmd[i] = re.sub(r"master-boot-aux=.*?(?=,controller-flash-data=)",
                                "master-boot-aux=" + args.aux, part)
                break
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    if args.no_adc:
        cmd = bp.strip_globals(cmd, "nikon-z72-controller-adc.")
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]

    qtest_port = args.port_base
    qmp_port = args.port_base + 1
    hmp_port = args.port_base + 7
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]
    for prop in (
        "nikon-z72-boot-config.frame-start=on",
        "nikon-z72-input-config.laboratory-metadata-completion=on",
        "nikon-z72-boot-config.input-command-completion=on",
        "nikon-z62-sensor-config.laboratory-mode-completion=on",
        "nikon-z62-sensor-config.laboratory-frame-completion=on",
        "nikon-z62-sensor-config.laboratory-init-completion=on",
        "nikon-z62-sensor-config.init-delay-ns=250000000",
    ):
        cmd += ["-global", prop]

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\gic.txt"), "wb")
    trace_path = ""
    if args.trace:
        trace_path = os.path.join(args.out, "gic.trace")
        for pattern in [p for p in args.trace.split(",") if p]:
            cmd += ["-trace", "enable=" + pattern]
        cmd += ["-D", trace_path]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=bp.env(), cwd=bp.ROOT)
    t0 = time.time()
    report = {}
    try:
        hmp = bp.Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = bp.instances(listing, "nikon-z72-controller-gpio")
        configs = bp.instances(listing, "nikon-z72-boot-config")
        wire = bp.Qtest(qtest_port)
        for bank, pin, level in bp.GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        for level in (0, 1):
            wire.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                     % (configs[0], level))
        print("  board armed")

        shots = sorted(float(x) for x in args.at.split(",") if x)
        next_menu = args.menu_start if args.menu_start > 0 else None
        release_at = None
        while shots or next_menu is not None or release_at is not None:
            now = time.time() - t0
            shot_due = shots[0] if shots else 1e9
            menu_due = next_menu if next_menu is not None else 1e9
            rel_due = release_at if release_at is not None else 1e9
            if min(shot_due, menu_due, rel_due) > now:
                time.sleep(min(0.4, max(0.05,
                             min(shot_due, menu_due, rel_due) - now)))
                continue
            if rel_due <= now:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1" % bp.MENU_PIN)
                print("t=%3.0fs MENU release" % now)
                release_at = None
                continue
            if menu_due <= now:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0" % bp.MENU_PIN)
                print("t=%3.0fs MENU press" % now)
                release_at = menu_due + args.menu_hold
                next_menu = (menu_due + args.menu_every
                             if args.menu_every > 0 else None)
                continue
            mark = shots.pop(0)
            print("t=%3.0fs" % now)
            report[int(mark)] = snapshot(hmp, "t=%.0fs" % now)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    json.dump(report, open(os.path.join(args.out, "gic.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
