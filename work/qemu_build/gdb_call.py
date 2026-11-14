"""Call a firmware function from outside and read its return value.

The PTP stack normally gets its commands from USB, which this machine does not
model. But the dispatcher is an ordinary function: 0x104a981c(code) looks up a
handler, 0x104a95d8(handler, packet) runs it. If we can call the first one and
get a non-null handler for 0x9207, the capture command genuinely exists in this
firmware and can be driven without a USB stack.

Mechanics: halt, save registers, set r0 = arg and pc = function, point lr at a
trap address with a breakpoint on it, resume, wait for the trap, read r0,
restore everything. CPSR's T bit is set for Thumb targets - getting that wrong
lands in Undefined, which cost a debugging session before.

  python gdb_call.py --func 0x104a981c --arg 0x9207 --thumb
  python gdb_call.py --func 0x104a981c --arg 0x1002 --thumb
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
import cap_read_probe as cr

TRAP = 0x104C9680          # dispatcher entry (image base 0x10020000, not 0x10000000)


def regs_of(rsp):
    g = rsp.cmd("g")
    if not g or len(g) < 17 * 8:
        return None
    return [int.from_bytes(bytes.fromhex(g[i * 8:(i + 1) * 8]), "little")
            for i in range(17)]


def set_regs(rsp, regs):
    # The g/G packets carry the raw target-order bytes, not host-order hex.
    # "%08x" % value writes big-endian text and QEMU reads it back byte-swapped
    # (0xc188bc38 became 0x38bc88c1), which silently sends every call to a
    # garbage address. Always encode little-endian.
    rsp.cmd("G" + "".join((v & 0xFFFFFFFF).to_bytes(4, "little").hex()
                          for v in regs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--func", type=lambda x: int(x, 0), required=True)
    ap.add_argument("--arg", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--arg1", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--thumb", action="store_true")
    ap.add_argument("--at", type=float, default=38.0)
    ap.add_argument("--port-base", type=int, default=5200)
    ap.add_argument("--aux", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--master", default="")
    ap.add_argument("--stop-at-func", action="store_true",
                    help="break at the callee entry to prove the jump landed")
    ap.add_argument("--selftest", action="store_true",
                    help="write the register block back unchanged and compare")
    args = ap.parse_args()

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
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]
    qtest_port = args.port_base
    hmp_port = args.port_base + 7
    gdb_port = args.port_base + 8
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port,
            "-gdb", "tcp:127.0.0.1:%d,server=on,wait=off" % gdb_port]

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\gdbcall.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=bp.ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=bp.env())
    t0 = time.time()
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
        while time.time() - t0 < args.at:
            time.sleep(0.4)

        rsp = cr.Rsp(gdb_port)
        print("qSupported ->", rsp.cmd("qSupported:multiprocess+"))
        rsp.s.sendall(b"\x03")
        rsp._read_packet(2.0)
        saved = regs_of(rsp)
        if not saved:
            raise SystemExit("cannot read registers")
        print("before: pc=%08x sp=%08x lr=%08x cpsr=%08x" %
              (saved[15], saved[13], saved[14], saved[16]))

        if args.selftest:
            g1 = rsp.cmd("g")
            print("G reply:", repr(rsp.cmd("G" + g1)))
            g2 = rsp.cmd("g")
            print("round-trip identical:", g1 == g2)
            if g1 and g2 and g1 != g2:
                for i in range(0, max(len(g1), len(g2)), 8):
                    if g1[i:i + 8] != g2[i:i + 8]:
                        print("  reg %d: %s -> %s" %
                              (i // 8, g1[i:i + 8], g2[i:i + 8]))
            rsp.send_only("c")
            return

        trap = rsp.cmd("Z0,%x,2" % TRAP)
        print("trap at 0x%08x -> %r" % (TRAP, trap))
        if args.stop_at_func:
            r = rsp.cmd("Z0,%x,2" % (args.func & ~1))
            print("entry bp at 0x%08x -> %r" % (args.func & ~1, r))

        regs = list(saved)
        regs[0] = args.arg
        regs[1] = args.arg1
        regs[13] = saved[13] & ~7
        regs[14] = TRAP | (1 if args.thumb else 0)
        cpsr = saved[16]
        if args.thumb:
            # The guest sits in ARM state, and writing CPSR.T together with PC
            # in one G packet does not reliably refetch in Thumb. Plant a tiny
            # ARM stub (`bx r12`) on the stack and enter through it instead.
            scratch = (saved[13] - 0x200) & ~3
            stub = (0xE12FFF1C).to_bytes(4, "little").hex()
            rsp.cmd("M%x,4:%s" % (scratch, stub))
            back = rsp.cmd("m%x,4" % scratch)
            print("trampoline at 0x%08x: bx r12 -> 0x%08x (readback %r)" %
                  (scratch, args.func | 1, back))
            regs[12] = args.func | 1
            regs[15] = scratch
            regs[16] = cpsr & ~0x20
        else:
            regs[15] = args.func & ~1
            regs[16] = cpsr & ~0x20
        set_regs(rsp, regs)
        print("call 0x%08x(0x%x, 0x%x) thumb=%s" %
              (args.func, args.arg, args.arg1, args.thumb))
        rsp.send_only("c")

        hit = False
        landed = None
        end = time.time() + 6.0
        while time.time() < end:
            pkt = rsp._read_packet(0.5)
            if pkt is None:
                continue
            if pkt.startswith(("T", "S")):
                if args.stop_at_func and landed is None:
                    snap = regs_of(rsp)
                    landed = snap[15] if snap else None
                    ok = landed is not None and (landed & ~1) == (args.func & ~1)
                    if snap:
                        print("  first stop at 0x%08x -> %s  lr=%08x sp=%08x r0=%08x" %
                              (landed or 0,
                               "ENTRY REACHED" if ok else "NOT THE ENTRY",
                               snap[14], snap[13], snap[0]))
                    else:
                        print("  first stop, registers unreadable")
                    rsp.cmd("z0,%x,2" % (args.func & ~1))
                    rsp.send_only("c")
                    continue
                hit = True
                break
        if not hit:
            # Stop the guest so the registers are readable, and say where it
            # ended up - a PC inside the callee means it is still running, a
            # PC near zero means the call went off the rails.
            rsp.s.sendall(b"\x03")
            print("halt after timeout ->", repr(rsp._read_packet(2.0)))
        after = regs_of(rsp)
        print("trap hit:", hit)
        if after:
            print("after : pc=%08x sp=%08x lr=%08x" %
                  (after[15], after[13], after[14]))
            print("RETURN r0 = 0x%08x  (%d)" % (after[0], after[0]))
            if after[0]:
                print("  looks like a handler pointer; thumb bit = %d" %
                      (after[0] & 1))
        rsp.cmd("z0,%x,2" % TRAP)
        set_regs(rsp, saved)
        rsp.send_only("c")
        print("registers restored")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
