"""Drive a nikon-z6ii run through QEMU's human monitor.

gdb on this host has no ARM target description, so the monitor is the only way
in. Two jobs:
 - mon_probe: let the boot code run, then pause it and read the cores' state.
   The firmware's exception handlers park themselves in a "b ." loop, so the
   interesting data is the context block they build: sp+0x34 is the faulting
   mode's SP and sp+0x38 its LR (= faulting instruction + 8).
 - --poll: sample a DDR cell through pause/cont, to ask whether the boot path
   seeds it at all. For "which instruction wrote it" there is no HMP answer
   (this build has neither `watch` nor `sstep`, and its gdbstub answers `Z,2`
   with an empty packet), so use gdb_probe.py, which replays the store stream
   out of the -d exec/in_asm log instead.

Z6II_KERNEL overrides the image handed to -kernel, which is how the same board
description gets run with sla in place of mas. Z6II_CPU picks the core.

  python mon_probe.py --wait 8 --addrs 0xc18ad850
  Z6II_KERNEL=.../eg1850_sla_01700 python mon_probe.py --wait 8
  Z6II_CPU=cortex-a15 python mon_probe.py --wait 8
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MAS = os.environ.get("Z6II_KERNEL",
                     r"D:\z6ii_fw_lab\extracted\eg1850_mas_01700")
TRACE = os.environ.get("Z6II_TRACE",
                       r"D:\z6ii_fw_lab\work\qemu_build\run\mon_trace.log")
PORT = int(os.environ.get("Z6II_MON_PORT", "4445"))
CPU = os.environ.get("Z6II_CPU", "cortex-a15")
# qemu-system-arm.exe is linked against MSYS2's libusb-1.0.dll, so the MSYS2
# bin dirs must be on PATH or the process dies with DLL_NOT_FOUND before the
# monitor ever opens.
MSYS = r"D:\z6ii_fw_lab\tools\msys64"


def qemu_env():
    import os
    env = dict(os.environ)
    env["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                            os.path.join(MSYS, "usr", "bin"),
                            os.path.join(MSYS, "bin"),
                            env.get("PATH", "")])
    return env


def strip_iac(buf):
    out, i = [], 0
    while i < len(buf):
        if buf[i] == 0xFF and i + 2 < len(buf):
            i += 3
            continue
        if buf[i] >= 0x80:
            i += 1
            continue
        out.append(buf[i])
        i += 1
    return bytes(out)


def send(sock, hmp, wait=0.4):
    sock.sendall((hmp + "\n").encode())
    time.sleep(wait)
    sock.setblocking(False)
    buf = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except (BlockingIOError, OSError):
            break
        if not chunk:
            break
        buf += chunk
    # The monitor echoes its own prompt + the typed command one char at a time;
    # keep only the lines that are real output.
    lines = []
    for line in strip_iac(buf).decode("latin1").replace("\r", "").splitlines():
        if line.startswith("(qemu) ") or "QEMU 11 monitor" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def brief(text):
    """Drop the VFP register dump: `info registers` is 90% of it."""
    vfp = re.compile(r"^[sd][0-9]{1,2}=[0-9a-f]+$")
    keep = []
    for line in text.splitlines():
        f = line.split()
        if f and all(vfp.match(part) for part in f):
            continue
        if line.startswith("FPSCR"):
            continue
        keep.append(line)
    return "\n".join(keep)


def field(text, key):
    for line in text.splitlines():
        for part in line.split():
            if part.startswith(key + "="):
                return "0x" + part.split("=", 1)[1]
    return None


def sample_until(sock, addr, wait, interval):
    """Pause/cont until the word at addr stops being zero.

    Only coarse: a pause/cont round trip costs ~50ms of wall clock, during
    which TCG retires millions of instructions, so this answers "does this cell
    ever get seeded, and does the machine still work when I look at it right
    after" rather than "which instruction wrote it".
    """
    pat = re.compile(r"[0-9a-f]+:\s+0x([0-9a-f]+)")
    end = time.time() + wait
    send(sock, "stop", 0.3)
    while time.time() < end:
        send(sock, "cont", 0.02)
        time.sleep(interval)
        send(sock, "stop", 0.02)
        m = pat.search(send(sock, "xp/1xw %s" % addr, 0.05))
        if m and int(m.group(1), 16):
            return int(m.group(1), 16)
    return None


def connect(deadline):
    end = time.time() + deadline
    while time.time() < end:
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), 0.2)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        except OSError:
            time.sleep(0.2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=8.0)
    ap.add_argument("--smp", type=int, default=2)
    ap.add_argument("--addrs", default="", help="comma separated DDR addresses "
                    "to dump as words after the cores have stopped")
    ap.add_argument("--watch", default="", help="physical address to put a "
                    "write watchpoint on before the run starts")
    ap.add_argument("--poll", default="", help="physical word to sample by "
                    "pause/cont cycles until it turns non-zero; catches which "
                    "code seeds a DDR cell (HMP here has no `watch`)")
    ap.add_argument("--interval", type=float, default=0.005)
    ap.add_argument("--log", default="int,unimp")
    args = ap.parse_args()

    proc = subprocess.Popen(
        [QEMU, "-M", "nikon-z6ii", "-cpu", CPU, "-smp", str(args.smp),
         "-kernel", MAS, "-d", args.log, "-D", TRACE,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % PORT,
         "-display", "none"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=qemu_env())
    sock = None
    try:
        sock = connect(6.0)
        if sock is None:
            print("monitor never came up", file=sys.stderr)
            return 1
        send(sock, "cpu 0", 0.2)
        if args.poll:
            word = sample_until(sock, args.poll, args.wait, args.interval)
            if word is None:
                print("no change seen at %s" % args.poll)
            else:
                print("%s became 0x%08x" % (args.poll, word))
            for cpu in range(args.smp):
                send(sock, "cpu %d" % cpu, 0.2)
                print("===== CPU%d =====" % cpu)
                print(brief(send(sock, "info registers", 0.8)))
            for addr in [a for a in args.addrs.split(",") if a]:
                print("-- %s --" % addr)
                print(send(sock, "xp/32xw %s" % addr, 0.6))
            return 0
        if args.watch:
            print(send(sock, "watch %s" % args.watch, 0.3))
            # Wait for the watchpoint to halt the machine.
            end = time.time() + args.wait
            while time.time() < end:
                time.sleep(0.5)
                status = send(sock, "info status", 0.3)
                if "paused" in status:
                    print("-- halted by watchpoint --")
                    break
        else:
            time.sleep(args.wait)
            send(sock, "stop", 0.8)

        for cpu in range(args.smp):
            send(sock, "cpu %d" % cpu, 0.2)
            regs = send(sock, "info registers", 0.8)
            print("===== CPU%d =====" % cpu)
            print(brief(regs))
            if args.watch:
                continue
            sp = field(regs, "R13")
            if sp:
                print("-- context block at %s --" % sp)
                print(send(sock, "xp/17xw %s" % sp, 0.6))
        for addr in [a for a in args.addrs.split(",") if a]:
            print("-- %s --" % addr)
            print(send(sock, "xp/32xw %s" % addr, 0.6))
    finally:
        if sock:
            sock.close()
        proc.kill()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
