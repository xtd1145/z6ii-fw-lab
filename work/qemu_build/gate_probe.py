"""Prove the release gate is sensitive to the acknowledgement value.

Background: the release helper in the mas image at 0x10200e57 stores 0xA1 to
0x04287010 and then polls bit 0 of 0x04287004:

    0x10200e5f  str  r0,[r1,#0x10]   ; ack
    0x10200e61  ldr  r0,[r1,#4]      ; status
    0x10200e65  beq  0x10200e61      ; spin while bit0 == 0

The model used to answer "acknowledged" on every read, so no ack value could
ever fail and the v9 black brick (firmware parked at 0x10200E60) was
invisible. This probe runs the guest twice:

  default        gate-ack-strict=on   -> must boot, gate-ack-ok > 0, no card
  --starve       gate-ack-starve=on   -> must park at 0x10200e61 forever

The second run is the regression test that v9 did not have.

  python gate_probe.py --at 40
  python gate_probe.py --at 40 --starve
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from board_probe import (Hmp, Qtest, Qmp, env, instances, CMD, QEMU, ROOT,
                         GPIO_INPUTS)

# The firmware's release poll loop. Both the loop head (ldr) and the branch
# live here; a parked guest shows up inside this window.
RELEASE_POLL = range(0x10200E5C, 0x10200E6C)


def qom_int(qmp, path, name):
    msg = qmp.cmd("qom-get", path=path, property=name)
    ret = msg.get("return")
    if isinstance(ret, dict):          # qom-get returns {"return": n}
        return ret
    return ret


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=40.0)
    ap.add_argument("--starve", action="store_true",
                    help="turn on gate-ack-starve to emulate a body that never "
                         "acknowledges")
    ap.add_argument("--no-strict", action="store_true",
                    help="gate-ack-strict=off (old always-acknowledged model)")
    ap.add_argument("--port-base", type=int, default=4560)
    args = ap.parse_args()

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    for drop in ("-monitor",):
        if drop in cmd:
            i = cmd.index(drop)
            del cmd[i:i + 2]
    for prop in ("nikon-z72-boot-config.frame-start=on",
                 "nikon-z72-input-config.laboratory-metadata-completion=on",
                 "nikon-z72-boot-config.input-command-completion=on"):
        cmd += ["-global", prop]
    if args.starve:
        cmd += ["-global", "nikon-z72-boot-config.gate-ack-starve=on"]
    if args.no_strict:
        cmd += ["-global", "nikon-z72-boot-config.gate-ack-strict=off"]

    hmp_port = args.port_base + 7
    qmp_port = args.port_base + 1
    qtest_port = args.port_base
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]

    logp = os.path.join(ROOT, r".local\z62-smoke\gate.txt")
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env())
    t0 = time.time()
    label = "starve" if args.starve else ("no-strict" if args.no_strict
                                          else "strict")
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = instances(listing, "nikon-z72-controller-gpio")
        wire = Qtest(qtest_port)
        for bank, pin, level in GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        print("[%s] gpio asserted, running %.0fs" % (label, args.at))
        time.sleep(args.at)

        hmp.cmd("stop")
        # Where is every engine core?
        pcs = {}
        for cpu in range(4):
            hmp.cmd("cpu %d" % cpu)
            regs = hmp.cmd("info registers")
            m = re.search(r"R15=([0-9a-f]{8})", regs)
            pcs[cpu] = int(m.group(1), 16) if m else None
        hmp.cmd("cpu 0")
        hmp.cmd("cont")

        qmp = Qmp(qmp_port)
        ok = qom_int(qmp, "/machine/unattached/device[0]", "gate-ack-ok")
        # The boot-config instance index is not stable across machines; try a
        # few and keep the first numeric answer.
        if ok is None:
            for idx in range(0, 6):
                ok = qom_int(qmp, "/machine/unattached/device[%d]" % idx,
                             "gate-ack-ok")
                if ok is not None:
                    break
        print("\n=== 结果 (%s) ===" % label)
        print("PCs: " + "  ".join("cpu%d=0x%08x" % (c, p) if p else "cpu%d=?"
                                  for c, p in pcs.items()))

        parked = [c for c, p in pcs.items()
                  if p is not None and p in RELEASE_POLL]
        if parked:
            print("★ 停在 release 轮询循环: cpu%s —— 与 v9 黑砖同一点" %
                  ",".join(str(c) for c in parked))
        else:
            print("未停在 release 轮询循环")

        # Screenshot-free health check: the GPU counters prove the foreground
        # came up.
        for name in ("successful-uploads", "rendered-commands"):
            out = hmp.cmd("qom-get /machine/master-graphics " + name)
            print("  gpu %s = %s" % (name, out.strip().splitlines()[-1]))

        print("\n日志: %s" % logp)
        print("过滤: grep -aE \"Z62_GATE\" %s" % logp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
