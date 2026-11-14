"""Break on a firmware address from the very first instruction.

The buffer-registration branch at 0x1024a394 only runs if the firmware gets
that far; watching memory afterwards cannot tell "never registered" from
"registered and never used". Starting the CPU halted, planting a breakpoint,
then letting the board power up answers that directly.

  python bp_probe.py --bp 0x1024a394 --stop 45
  python bp_probe.py --bp 0x1033d3be --bp2 0x1024a39a --stop 60
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_probe as bp
import cap_read_probe as cr


def decode_pc(rsp):
    g = rsp.cmd("g")
    if g and len(g) >= 16 * 8:
        return int.from_bytes(bytes.fromhex(g[15 * 8:16 * 8]), "little")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bp", type=lambda x: int(x, 0), required=True)
    ap.add_argument("--bp2", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--stop", type=float, default=45.0)
    ap.add_argument("--port-base", type=int, default=5040)
    ap.add_argument("--aux", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--max-hits", type=int, default=12)
    ap.add_argument("--keys", default="", help="comma separated expander pins to press in turn")
    ap.add_argument("--key-start", type=float, default=25.0)
    ap.add_argument("--key-gap", type=float, default=3.0)
    ap.add_argument("--key-hold", type=float, default=0.3)
    ap.add_argument("--gpio-scan", action="store_true",
                    help="pulse every unused controller GPIO input in turn")
    ap.add_argument("--gpio-scan-start", type=float, default=22.0)
    ap.add_argument("--gpio-high", type=float, default=0.5)
    ap.add_argument("--gpio-low", type=float, default=0.7)
    ap.add_argument("--gpio-max-pin", type=int, default=16)
    args = ap.parse_args()

    cmd = [c for c in json.load(open(bp.CMD))]
    cmd[0] = bp.QEMU
    if args.aux:
        import re
        for i, part in enumerate(cmd):
            if "master-boot-aux=" in part:
                cmd[i] = re.sub(r"master-boot-aux=.*?(?=,controller-flash-data=)",
                                "master-boot-aux=" + args.aux, part)
                break
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]
    qtest_port = args.port_base
    hmp_port = args.port_base + 7
    gdb_port = args.port_base + 8
    cmd += ["-S",
            "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port,
            "-gdb", "tcp:127.0.0.1:%d,server=on,wait=off" % gdb_port]

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\bp.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=bp.ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=bp.env())
    t0 = time.time()
    hits = []
    try:
        hmp = bp.Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = bp.instances(listing, "nikon-z72-controller-gpio")
        configs = bp.instances(listing, "nikon-z72-boot-config")
        wire = bp.Qtest(qtest_port)

        rsp = cr.Rsp(gdb_port)
        print("qSupported ->", rsp.cmd("qSupported:multiprocess+"))
        print("stop reason ->", repr(rsp._read_packet(1.0)))
        print("pc at reset -> 0x%08x" % (decode_pc(rsp) or 0))

        for addr, kind in ((args.bp, "Z0"), (args.bp2, "Z0")):
            if not addr:
                continue
            reply = rsp.cmd("%s,%x,2" % (kind, addr))
            print("%s,0x%08x,2 -> %r" % (kind, addr, reply))

        rsp.send_only("c")
        print("running; asserting power inputs")
        for bank, pin, level in bp.GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        for level in (0, 1):
            wire.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                     % (configs[0], level))

        end = time.time() + args.stop
        keys = [int(k) for k in args.keys.split(",") if k.strip()]
        key_i = 0
        key_due = args.key_start
        key_release = None
        scan = []
        if args.gpio_scan:
            used = {(b, p) for b, p, _ in bp.GPIO_INPUTS}
            scan = [(b, p) for b in range(8) for p in range(args.gpio_max_pin)
                    if (b, p) not in used]
        scan_i = 0
        scan_due = args.gpio_scan_start
        scan_release = None
        while time.time() < end and len(hits) < args.max_hits:
            now = time.time() - t0
            if args.gpio_scan:
                if scan_release is not None and now >= scan_release:
                    b, p = scan[scan_i - 1]
                    wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d 0"
                             % (gpios[b], p))
                    scan_release = None
                    scan_due = now + args.gpio_low
                elif (scan_release is None and scan_i < len(scan) and
                      now >= scan_due):
                    b, p = scan[scan_i]
                    wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d 1"
                             % (gpios[b], p))
                    print("  gpio bank %d pin %2d high (t=%.1fs)" % (b, p, now))
                    scan_release = now + args.gpio_high
                    scan_i += 1
            elif key_release is not None and now >= key_release:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1"
                         % keys[key_i - 1])
                key_release = None
                key_due = now + args.key_gap
            elif (key_release is None and key_i < len(keys) and now >= key_due):
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0"
                         % keys[key_i])
                print("  key pin %d pressed (t=%.1fs)" % (keys[key_i], now))
                key_release = now + args.key_hold
                key_i += 1
            pkt = rsp._read_packet(0.4)
            if pkt is None:
                continue
            if pkt.startswith(("T", "S")):
                if len(hits) == 1:
                    print("  stop packet:", pkt[:60])
                m = re.search(r"thread:p([0-9a-fA-F]+)\.([0-9a-fA-F]+)", pkt)
                if m:
                    who = "cpu%d" % int(m.group(2), 16)
                else:
                    m = re.search(r"core:([0-9a-fA-F]+)", pkt)
                    who = ("cpu%d" % int(m.group(1), 16)) if m else "cpu?"
                pc = decode_pc(rsp)
                hits.append(pc)
                if args.gpio_scan and scan_i:
                    tag = "bank%d.pin%d" % scan[scan_i - 1]
                elif key_i:
                    tag = "key%d" % keys[key_i - 1]
                else:
                    tag = "-"
                print("  hit #%d at 0x%08x (%s, t=%.1fs, after %s)" %
                      (len(hits), pc or 0, who, time.time() - t0, tag))
                rsp.dump_frame("callframe")
                rsp.send_only("c")
        print("\n=== %d hits in %.0fs ===" % (len(hits), args.stop))
        if not hits:
            print("断点未命中：固件没有走到 0x%08x" % args.bp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
