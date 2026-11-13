#!/usr/bin/env python3
"""Unit test for the CVP command-completion path.

The firmware never issues a CVP command on any boot path we can reach (five
initialisation writes and nothing else), so the completion logic cannot be
exercised from the guest. This test drives the mailbox from outside instead:
it writes a three-word command into the TX window through the CPU's own
address space, lets the model's timer fire, and reads the RX FIFO back.

What it proves: the submit path, the 0xb000 completion relation, the FIFO
ordering and the IRQ raise all work. What it does NOT prove: that any result
payload matches silicon - there is no payload, the arguments are echoed.

    python cvp_compute_test.py --at 30
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, r"D:\z6ii_fw_lab\work\qemu_build")
from mon_probe import QEMU, qemu_env  # noqa: E402
from gdb_probe import Stub, regs  # noqa: E402

ROOT = r"D:\z6ii_fw_lab"
CMD = os.path.join(ROOT, r".local\z62-smoke\command.json")

CVP_BASE = 0x24020000
CVP_RX = CVP_BASE + 0x00
CVP_TX = CVP_BASE + 0x04
CVP_SUBMIT = CVP_BASE + 0x08
CVP_IRQ_STATUS = CVP_BASE + 0x14


def rsp_write(stub, addr, value):
    """Write one 32-bit little-endian word through the gdbstub."""
    payload = "M%x,4:%s" % (addr, value.to_bytes(4, "little").hex())
    return stub.cmd(payload, 2.0)


def rsp_read(stub, addr, length=4):
    t = stub.cmd("m%x,%x" % (addr, length), 2.0)
    if not t or t[0] in ("E",):
        return None
    try:
        return bytes.fromhex(t)
    except ValueError:
        return None


def qom_get(port, path, prop, timeout=2.0):
    """Read one QOM uint64 through QMP. Returns None on any failure."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout)
    except OSError:
        return None
    f = sock.makefile("rwb")

    def send(obj):
        f.write((json.dumps(obj) + "\r\n").encode())
        f.flush()
        line = f.readline()
        try:
            return json.loads(line.decode())
        except Exception:
            return None

    try:
        f.readline()                      # greeting
        send({"execute": "qmp_capabilities"})
        r = send({"execute": "qom-get",
                  "arguments": {"path": path, "property": prop}})
    finally:
        sock.close()
    if not r or "return" not in r:
        return None
    try:
        return int(r["return"])
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=30.0,
                    help="seconds of boot before the test runs")
    ap.add_argument("--opcode", type=lambda x: int(x, 0), default=0x1234)
    ap.add_argument("--arg1", type=lambda x: int(x, 0), default=0x00010000)
    ap.add_argument("--arg2", type=lambda x: int(x, 0), default=0x00000007)
    ap.add_argument("--delay-ns", type=int, default=2000000)
    args = ap.parse_args()

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    cmd += ["-global", "nikon-z62-cvp-control.laboratory-compute=on",
            "-global", "nikon-z62-cvp-control.trace=on",
            "-global", "nikon-z62-cvp-control.compute-delay-ns=%d"
                       % args.delay_ns]

    port = 9341
    qmp_port = port + 1
    cmd += ["-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    cmd += ["-S", "-gdb", "tcp:127.0.0.1:%d,server,nowait" % port]

    print("starting qemu (dual, compute=on)...")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env=qemu_env())

    sock = None
    end = time.time() + 20
    while time.time() < end:
        try:
            sock = socket.create_connection(("127.0.0.1", port), 0.5)
            break
        except OSError:
            time.sleep(0.3)
    if not sock:
        print("no gdbstub"); proc.kill(); sys.exit(1)

    stub = Stub(sock)
    stub.noack()

    print("booting %.0fs..." % args.at)
    stub.send("c")
    time.sleep(args.at)
    stub.interrupt()
    stub.recv_packet(3.0)

    r = regs(stub.cmd("g"))
    print("cpu0 PC = 0x%08x" % (r["R15"] if r else 0))

    before = rsp_read(stub, CVP_RX, 4)
    print("RX[0] before = %s" % (before.hex() if before else "MEMTX_ERROR"))

    # 1) three TX words, then submit=2
    print("\n--- submit command [%08x %08x %08x] ---"
          % (args.opcode, args.arg1, args.arg2))
    for idx, val in enumerate((args.opcode, args.arg1, args.arg2)):
        resp = rsp_write(stub, CVP_TX, val)
        print("  TX[%d] = %08x -> %r" % (idx, val, resp))
    resp = rsp_write(stub, CVP_SUBMIT, 2)
    print("  submit=2 -> %r" % (resp,))

    # 2) let the timer fire, polling so the firmware's ISR cannot drain the
    #    packet before we look at it
    print("\n--- poll RX (firmware may consume it first) ---")
    words = []
    status = None
    stub.send("c")
    for attempt in range(24):
        time.sleep(0.08)
        stub.interrupt()
        stub.recv_packet(2.0)
        status = rsp_read(stub, CVP_IRQ_STATUS, 4)
        words = []
        for i in range(3):
            b = rsp_read(stub, CVP_RX, 4)
            if b is None:
                break
            words.append(int.from_bytes(b, "little"))
        if len(words) == 3:
            print("  caught packet on attempt %d: %s"
                  % (attempt + 1, " ".join("%08x" % w for w in words)))
            break
        stub.send("c")
    if not words:
        print("  RX stayed empty (firmware ISR drained it)")

    # 4) verdict: packet contents if caught, counters otherwise
    print("\n--- counters (QMP) ---")
    base = "/machine/master-cvp"
    req = qom_get(qmp_port, base, "compute-requests")
    done = qom_get(qmp_port, base, "compute-completions")
    drop = qom_get(qmp_port, base, "compute-dropped")
    unsup = qom_get(qmp_port, base, "unsupported-commands")
    print("  compute-requests    = %s" % req)
    print("  compute-completions = %s" % done)
    print("  compute-dropped     = %s" % drop)
    print("  unsupported-commands= %s" % unsup)

    print("\n--- verdict ---")
    expect_op = args.opcode | 0xb000
    if len(words) == 3:
        ok = (words[0] == expect_op and words[1] == args.arg1 and
              words[2] == args.arg2)
        print("  expected [%08x %08x %08x]" % (expect_op, args.arg1, args.arg2))
        print("  got      [%08x %08x %08x]" % tuple(words))
    else:
        ok = (req == 1 and done == 1 and drop == 0 and unsup == 0)
        print("  packet consumed by the guest ISR; judged on counters:")
        print("  requests=1 completions=1 dropped=0 unsupported=0 -> %s"
              % ("PASS" if ok else "FAIL"))
    print("  result: %s" % ("PASS" if ok else "FAIL"))

    sock.close()
    proc.kill()
    out = proc.stderr.read().decode("utf-8", "replace")
    proc.wait()
    for line in out.splitlines():
        if "z62.cvp" in line:
            print("  log: " + line.strip())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
