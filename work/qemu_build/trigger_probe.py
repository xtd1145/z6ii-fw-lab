"""Inject DDR/GIC writes through QEMU's gdbstub and observe whether the
firmware leaves its idle spin.

Reads (CPU registers, DDR, GIC) go through the HMP monitor so we can pick the
core; writes go through the gdbstub `M` packet (the monitor has no memory
write).  The DDR region is identity-mapped, so writing VA 0xc2b476dc lands on
the same physical cell the firmware polls.

  python trigger_probe.py --write 0xc2b476dc=0x00000002
  python trigger_probe.py --write 0xc2b2a948=0xc2c15008
"""
import argparse
import socket
import subprocess
import sys
import time

sys.path.insert(0, r"D:\z6ii_fw_lab\work\qemu_build")
from mon_probe import (CPU, MAS, QEMU, TRACE, brief, field, qemu_env,  # noqa
                       send, strip_iac)
from gdb_probe import Stub

MON_PORT = 4455
GDB_PORT = 1237


def mon_connect(deadline):
    end = time.time() + deadline
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", MON_PORT), 0.3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError:
            time.sleep(0.2)
    return None


def gdb_connect(deadline):
    end = time.time() + deadline
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", GDB_PORT), 0.3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError:
            time.sleep(0.2)
    return None


def read_mem(stub, addr, nwords):
    text = stub.cmd("m%x,%x" % (addr, nwords * 4))
    if not text or all(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    data = bytes.fromhex(text)
    return [int.from_bytes(data[i:i + 4], "little")
            for i in range(0, len(data) - 3, 4)]


def write_mem(stub, addr, words):
    data = b"".join(w.to_bytes(4, "little") for w in words)
    return stub.cmd("M%x,%x:%s" % (addr, len(data), data.hex()))


def mon_pc(msock, cpu):
    send(msock, "cpu %d" % cpu, 0.2)
    regs = send(msock, "info registers", 0.6)
    pc = field(regs, "R15")
    sp = field(regs, "R13")
    return pc, sp, brief(regs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="append", default=[],
                    help="addr=hex,hex,.. ; inject these words")
    ap.add_argument("--boot", type=float, default=4.0)
    ap.add_argument("--after", type=float, default=1.5)
    args = ap.parse_args()

    proc = subprocess.Popen(
        [QEMU, "-M", "nikon-z6ii", "-cpu", CPU, "-smp", "2",
         "-kernel", MAS, "-S",
         "-gdb", "tcp:127.0.0.1:%d,server,nowait" % GDB_PORT,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % MON_PORT,
         "-display", "none"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=qemu_env())

    msock = gsock = None
    try:
        msock = mon_connect(6.0)
        gsock = gdb_connect(6.0)
        if msock is None or gsock is None:
            print("monitor/gdbstub never came up", file=sys.stderr)
            return 1
        stub = Stub(gsock)
        try:
            stub.cmd("QStartNoAckMode")
        except Exception:
            pass

        # let the guest run out of the -S state and reach the idle spin
        send(msock, "cont", 0.2)
        time.sleep(args.boot)
        send(msock, "stop", 0.5)

        print("=== before injection ===")
        for cpu in (0, 1):
            pc, sp, _ = mon_pc(msock, cpu)
            print("CPU%d PC=%s SP=%s" % (cpu, pc, sp))

        addr_val = 0xc2b476dc
        words = read_mem(stub, addr_val, 1)
        print("[0xc2b476dc] = %s" % (" ".join("0x%08x" % w for w in words)
                                     if words else words))
        words = read_mem(stub, 0xc2b2a944, 2)
        print("[0xc2b2a944] = %s" % (" ".join("0x%08x" % w for w in words)
                                     if words else words))

        for spec in args.write:
            addr_s, val_s = spec.split("=")
            addr = int(addr_s, 16)
            vals = [int(x, 16) for x in val_s.split(",")]
            reply = write_mem(stub, addr, vals)
            print("write %s = %s -> %r" % (addr_s, val_s, reply))

        send(msock, "cont", 0.2)
        time.sleep(args.after)
        send(msock, "stop", 0.5)

        print("=== after injection ===")
        for cpu in (0, 1):
            pc, sp, regs = mon_pc(msock, cpu)
            print("CPU%d PC=%s SP=%s" % (cpu, pc, sp))
            print(regs)
        words = read_mem(stub, addr_val, 1)
        print("[0xc2b476dc] = %s" % (" ".join("0x%08x" % w for w in words)
                                     if words else words))
    finally:
        for s in (msock, gsock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        proc.kill()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
