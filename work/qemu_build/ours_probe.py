"""Sample the local nikon-z6ii machine's kernel cells on a wall-clock timeline.

The upstream Z62 board is the oracle: at 60 s it still has every kernel cell at
zero and 0xC0000000 unmapped, so "all zero" is not by itself evidence that a run
is stuck. This samples the same cells over several minutes to separate "stalled"
from "has not got there yet".

  python ours_probe.py --wait 60,120,180,240,300,360 --out run/ours_timeline.txt
"""
import argparse
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mon_probe as M

CELLS = [0xC2B2A944, 0xC2B41604, 0xC2B2B5A8, 0xC18AD828, 0xC18AD850,
         0xC0000000, 0x0428002C]


def read_cell(sock, addr):
    out = M.send(sock, "xp/1xw 0x%08x" % addr, 0.15)
    for line in out.splitlines():
        if ":" in line and line.split(":")[0].strip().startswith("0x"):
            return line.split(":", 1)[1].strip().split()[0]
        if ":" in line and line.split(":")[0].strip().startswith("%08x" % addr):
            return line.split(":", 1)[1].strip().split()[0]
    return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", default="60,120,180,240,300,360")
    ap.add_argument("--port", type=int, default=4470)
    ap.add_argument("--out", default=r"run\ours_timeline.txt")
    args = ap.parse_args()

    cmd = [M.QEMU, "-M", "nikon-z6ii", "-cpu", M.CPU, "-kernel", M.MAS,
           "-smp", "2", "-display", "none", "-serial", "none",
           "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % args.port,
           "-d", "int", "-D", "run/ours_timeline_trace.log"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=M.qemu_env())
    deadline = time.time() + 30
    sock = None
    while sock is None and time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", args.port), 0.5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            time.sleep(0.3)
    if sock is None:
        raise SystemExit("monitor never came up")
    sock.setblocking(False)
    time.sleep(0.5)
    marks = [float(x) for x in args.wait.split(",")]
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as f:
        try:
            for mark in marks:
                while time.time() - t0 < mark:
                    time.sleep(0.5)
                M.send(sock, "stop", 0.4)
                pcs = []
                for cpu in range(2):
                    M.send(sock, "cpu %d" % cpu, 0.2)
                    out = M.brief(M.send(sock, "info registers", 0.6))
                    import re
                    m = re.findall(r"R15=([0-9a-f]{8})", out)
                    pcs.append(m[0] if m else "?")
                cells = " ".join("[0x%08x]=%s" % (c, read_cell(sock, c))
                                 for c in CELLS)
                f.write("t=%3.0fs cpu0=%s cpu1=%s %s\n" %
                        (time.time() - t0, pcs[0], pcs[1], cells))
                f.flush()
                M.send(sock, "cont", 0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
