"""Does the Z4 access watchpoint actually get installed?

Every previous "the firmware never touches the capture buffer" result rests on
gdbstub watchpoints. If the reply parser silently drops a leading ack byte, a
rejected Z packet looks exactly like a timeout, and the probe reports zero hits
whether or not the firmware ever reads the address. This sets a watchpoint on
the live stack pointer - which any function call must touch - and reports
whether it fires.

  python watch_selftest.py --port-base 4820
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_probe as bp
import cap_read_probe as cr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-base", type=int, default=4820)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--at", type=float, default=40.0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0)
    args = ap.parse_args()

    cmd = [c for c in json.load(open(bp.CMD))]
    cmd[0] = bp.QEMU
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    qtest_port = args.port_base
    hmp_port = args.port_base + 7
    gdb_port = args.port_base + 8
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port,
            "-gdb", "tcp:127.0.0.1:%d,server=on,wait=off" % gdb_port]

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\watch.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=bp.ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=bp.env())
    t0 = time.time()
    try:
        hmp = bp.Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = bp.instances(listing, "nikon-z72-controller-gpio")
        wire = bp.Qtest(qtest_port)
        for bank, pin, level in bp.GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))

        while time.time() - t0 < args.at:
            time.sleep(0.3)
        # Stop the guest so register reads are coherent.
        rsp = cr.Rsp(gdb_port)
        print("qSupported ->", rsp.cmd("qSupported:multiprocess+"))
        threads = []
        reply = rsp.cmd("qfThreadInfo") or ""
        while reply.startswith("m"):
            threads += reply[1:].split(",")
            reply = rsp.cmd("qsThreadInfo") or ""
        print("threads    ->", threads)
        rsp.s.sendall(b"\x03")
        print("halt      ->", repr(rsp._read_packet(3.0)))
        g = rsp.cmd("g")
        print("g len =", len(g) if g else g)
        if not g or len(g) < 16 * 8:
            raise SystemExit("register packet unreadable: %r" % (g,))
        regs = [int.from_bytes(bytes.fromhex(g[i * 8:(i + 1) * 8]), "little")
                for i in range(16)]
        print("r0..r3  = %s" % " ".join("%08x" % r for r in regs[0:4]))
        print("sp=%08x lr=%08x pc=%08x" % (regs[13], regs[14], regs[15]))

        target = args.addr if args.addr else regs[13] & ~3
        r = rsp.cmd("Z4,%x,%x" % (target, 4))
        print("Z4,0x%08x,4 -> %r" % (target, r))
        print("--- per-cpu probe ---")
        for tid in ("01.01", "01.02", "01.03", "01.04", "01.05"):
            sel = rsp.cmd("Hg" + tid)
            gg = rsp.cmd("g")
            if gg and len(gg) >= 16 * 8:
                pc = int.from_bytes(bytes.fromhex(gg[15 * 8:16 * 8]), "little")
                sp = int.from_bytes(bytes.fromhex(gg[13 * 8:14 * 8]), "little")
                print("  Hg%-6s -> %-4r pc=%08x sp=%08x" % (tid, sel, pc, sp))
            else:
                print("  Hg%-6s -> %-4r g=%r" % (tid, sel, (gg or "")[:20]))
        hits = 0
        end = time.time() + args.seconds
        pressed = False
        rsp.send_only("c")
        while time.time() < end:
            now = time.time()
            if not pressed and now > end - args.seconds + 1.0:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0"
                         % bp.MENU_PIN)
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1"
                         % bp.MENU_PIN)
                print("  MENU pulsed")
                pressed = True
            pkt = rsp._read_packet(0.5)
            if pkt is None:
                continue
            if pkt.startswith(("T", "S")):
                hits += 1
                if hits <= 6:
                    g = rsp.cmd("g")
                    pc = (int.from_bytes(bytes.fromhex(g[15 * 8:16 * 8]), "little")
                          if g and len(g) >= 16 * 8 else None)
                    print("  hit #%d pc=%s" % (hits, "0x%08x" % pc if pc else "?"))
                rsp.send_only("c")
        print("=== watchpoint hits on stack 0x%08x: %d ===" % (target, hits))
        print("verdict:", "WORKING" if hits else "NOT FIRING")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
