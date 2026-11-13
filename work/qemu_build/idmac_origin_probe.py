"""Report per-core origin counters for the modelled IDMAC channel.

The gdb Z4 watchpoint is planted on the selected CPU only, so a transfer
driven from the other core is invisible to it. This probe reads the model-side
counters that record which cpu_index armed each kick and polled each result
word, giving a second-core verdict that does not depend on gdb CPU selection.

  python idmac_origin_probe.py --at 30
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from board_probe import (Hmp, Qtest, Qmp, env, instances, CMD, QEMU, ROOT,
                         GPIO_INPUTS)

PROPS = ("master-role", "idmac-kicks", "idmac-reads",
         "idmac-kicks-cpu0", "idmac-kicks-cpu1",
         "idmac-reads-cpu0", "idmac-reads-cpu1",
         "frame-count", "capture-done-reads")


def qom(qmp, path, name):
    msg = qmp.cmd("qom-get", path=path, property=name)
    ret = msg.get("return")
    if isinstance(ret, dict):
        ret = ret.get("return")
    return ret


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=30.0)
    ap.add_argument("--port-base", type=int, default=4570)
    args = ap.parse_args()

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    for prop in ("nikon-z72-boot-config.frame-start=on",
                 "nikon-z72-input-config.laboratory-metadata-completion=on",
                 "nikon-z72-boot-config.input-command-completion=on",
                 "nikon-z72-boot-config.idmac-trace=on"):
        cmd += ["-global", prop]

    qtest_port = args.port_base
    qmp_port = args.port_base + 1
    hmp_port = args.port_base + 7
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]

    logp = os.path.join(ROOT, r".local\z62-smoke\idmac_origin.txt")
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env())
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = instances(listing, "nikon-z72-controller-gpio")
        configs = instances(listing, "nikon-z72-boot-config")
        wire = Qtest(qtest_port)
        for bank, pin, level in GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        print("inputs asserted, running %.0fs" % args.at)
        time.sleep(args.at)

        qmp = Qmp(qmp_port)
        print("\n=== IDMAC origin counters ===")
        for cfg in configs:
            path = "/machine/unattached/" + cfg
            print("\n[%s]" % cfg)
            for name in PROPS:
                print("  %-24s = %s" % (name, qom(qmp, path, name)))
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
