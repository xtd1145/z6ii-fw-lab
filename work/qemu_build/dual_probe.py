"""Pause a nikon-z62-dual-lab run and read every engine core's state.

mon_probe.py drives the single-engine nikon-z6ii board. The dual board needs
five firmware images, a controller and a pile of i2c devices before it will
boot, so its command line is long enough to live in one place here. What we
want out of it is the *slave* engine's idle spin - CPU2/CPU3 run the sla image
and its task queue address has not been located yet (the master's is
0xc2b2a944 + core*4).

  python dual_probe.py --wait 30
  python dual_probe.py --wait 30 --addrs 0x20134910,0xc2b2a944
  python dual_probe.py --wait 30 --greps "z62.tw"

Core numbering on this board: 0/1 = master (mas), 2/3 = slave (sla),
4 = controller (ex1850 Cortex-M4).
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
QEMU = os.path.join(ROOT, "tools", "src", "qemu", "build",
                    "qemu-system-arm.exe")
MSYS = os.path.join(ROOT, "tools", "msys64")
PORT = int(os.environ.get("Z62_DUAL_PORT", "4470"))

MACHINE = ",".join([
    "nikon-z62-dual-lab",
    "controller-firmware=fw_verified/ex1850_012000.bi",
    "controller-link=on",
    "controller-battery-peer=on",
    "master-boot-image=fw_verified/eg1850_mas_01700",
    "slave-boot-image=fw_verified/eg1850_sla_01700",
    "master-boot-aux=.local/parameters_lab/group17.bin@0x132c0000,,"
    ".local/parameters_lab/group18.bin@0x132e0000,,"
    ".local/parameters_lab/group32.bin@0x13e80000",
    "controller-flash-data=.local/z62-smoke/resident.bin",
])

DEVICES = [
    "-device", "nikon-z72-controller-register-bank,bus=controller-i2c0,address=79",
    "-device", "nikon-z72-controller-expander,bus=controller-i2c0,address=34,"
               "id=keys,interrupt-gpio=/machine/controller/keys-gpio,interrupt-pin=8",
    "-device", "nikon-z72-controller-register-bank,bus=controller-i2c0,"
               "address=107,initial-state=.local/z62-smoke/primary.bin",
    "-device", "nikon-z72-controller-register-bank,bus=controller-i2c0,"
               "address=13,initial-state=.local/z62-smoke/auxiliary.bin",
    "-device", "nikon-z72-controller-register-bank,bus=master-i2c0,address=104",
    "-device", "nikon-z72-controller-register-bank,bus=master-i2c1,address=104",
    "-device", "nikon-z72-controller-register-bank,bus=master-i2c2,address=104",
    "-device", "nikon-z72-controller-register-bank,bus=master-i2c3,address=104",
    "-global", "nikon-z72-controller-adc.channel0=1800",
    "-global", "nikon-z72-controller-adc.channel1=3001",
    "-global", "nikon-z72-controller-adc.channel3=3001",
]


def qemu_env():
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
    sock.sendall((hmp + "\r\n").encode())
    time.sleep(wait)
    sock.setblocking(False)
    buf = b""
    while True:
        try:
            chunk = sock.recv(1 << 20)
        except (BlockingIOError, OSError):
            break
        if not chunk:
            break
        buf += chunk
    sock.setblocking(True)
    lines = []
    for line in strip_iac(buf).decode("latin1").replace("\r", "").splitlines():
        if line.startswith("(qemu) ") or "QEMU 11 monitor" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def brief(text):
    vfp = re.compile(r"^[sd][0-9]{1,2}=[0-9a-f]+$")
    keep = []
    for line in text.splitlines():
        f = line.split()
        if f and all(vfp.match(p) for p in f):
            continue
        if line.startswith("FPSCR"):
            continue
        keep.append(line)
    return "\n".join(keep)


def reg(text, key):
    for line in text.splitlines():
        for part in line.split():
            if part.startswith(key + "="):
                return int(part.split("=", 1)[1], 16)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=30.0)
    ap.add_argument("--smp", type=int, default=4)
    ap.add_argument("--addrs", default="")
    ap.add_argument("--cpus", default="", help="cores to dump, e.g. 2,3")
    ap.add_argument("--log", default="")
    ap.add_argument("--trace", default=os.path.join(
        HERE, "run", "dual_trace.log"))
    args = ap.parse_args()

    argv = [QEMU, "-M", MACHINE]
    argv += ["-smp", "%d,maxcpus=5" % args.smp]
    argv += ["-display", "none", "-serial", "none", "-nic", "none"]
    argv += ["-icount", "shift=0,sleep=off"]
    argv += ["-global", "nikon-z72-boot-config.frame-sync=on"]
    argv += DEVICES
    for c in range(args.smp):
        argv += ["-device", "loader,addr=0x10020000,cpu-num=%d" % c]
    argv += ["-monitor", "tcp:127.0.0.1:%d,server,nowait" % PORT]
    if args.log:
        argv += ["-d", args.log, "-D", args.trace]

    print("booting dual board for %.0fs ..." % args.wait)
    proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=qemu_env())
    sock = None
    try:
        end = time.time() + 10
        while time.time() < end:
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), 0.3)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                break
            except OSError:
                if proc.poll() is not None:
                    print("qemu exited early rc=%s" % proc.returncode)
                    return 1
                time.sleep(0.2)
        if sock is None:
            print("monitor never came up")
            return 1
        send(sock, "stop", 0.3)
        # Let it run, then stop.
        send(sock, "cont", 0.05)
        time.sleep(args.wait)
        send(sock, "stop", 0.8)

        cpus = ([int(x) for x in args.cpus.split(",")] if args.cpus
                else list(range(args.smp)))
        for cpu in cpus:
            send(sock, "cpu %d" % cpu, 0.2)
            regs = send(sock, "info registers", 1.0)
            pc = reg(regs, "R15") if regs else None
            print("===== CPU%d  PC=%s =====" % (
                cpu, "0x%08x" % pc if pc is not None else "?"))
            print(brief(regs))
            if pc is not None:
                print("-- around PC --")
                print(send(sock, "xp/12xw 0x%x" % (pc & ~0xf), 0.5))
        for addr in [a for a in args.addrs.split(",") if a]:
            print("-- %s --" % addr)
            print(send(sock, "xp/16xw %s" % addr, 0.6))
    finally:
        if sock:
            sock.close()
        proc.kill()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
