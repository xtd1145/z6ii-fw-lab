"""Sample the upstream Z62 machine's DDR through HMP while it boots.

The point is to use a known-good model as an oracle: the firmware is the same
image our nikon-z6ii machine runs, so the runtime task queue / task table cells
live at the same addresses. If they turn non-zero here and stay zero in ours,
the difference is in what the board provides, not in the firmware.

  python upstream_probe.py --wait 30,90,180
"""
import argparse
import json
import os
import socket
import subprocess
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = r"D:\z6ii_fw_lab\tools\msys64"
CMD = r"D:\z6ii_fw_lab\.local\z62-smoke\command.json"
CELLS = [0xC2B2A944, 0xC2B41604, 0xC2B2B5A8, 0xC2B2B5AC,
         0xC18AD828, 0xC18AD82C, 0xC18AD8B0, 0xC0000000]


def env():
    e = dict(os.environ)
    e["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                          os.path.join(MSYS, "usr", "bin"),
                          os.path.join(MSYS, "bin"), e.get("PATH", "")])
    return e


class Mon:
    def __init__(self, port):
        deadline = time.time() + 30
        while True:
            try:
                self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.s.settimeout(10)
        self.buf = b""
        self._read_until_prompt()

    def _read_until_prompt(self):
        while b"(qemu)" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("monitor closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"(qemu)")
        return out

    def cmd(self, text):
        self.s.sendall((text + "\n").encode())
        return self._read_until_prompt().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", default="30,90,180")
    ap.add_argument("--port", type=int, default=4460)
    args = ap.parse_args()

    cmd = json.load(open(CMD))
    cmd[0] = QEMU
    cmd += ["-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % args.port]
    log = open(r"D:\z6ii_fw_lab\.local\z62-smoke\oracle.txt", "wb")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env(), cwd=r"D:\z6ii_fw_lab")
    marks = [float(x) for x in args.wait.split(",")]
    t0 = time.time()
    try:
        mon = Mon(args.port)
        for mark in marks:
            while time.time() - t0 < mark:
                time.sleep(0.5)
            mon.cmd("stop")
            print("=== t=%.0fs ===" % (time.time() - t0))
            for cpu in range(4):
                out = mon.cmd("cpu %d" % cpu) + mon.cmd("info registers")
                for line in out.splitlines():
                    if line.startswith(("R15=", "PC=")):
                        print("  cpu%d %s" % (cpu, line.strip()))
            for cell in CELLS:
                out = mon.cmd("xp /1wx 0x%08x" % cell)
                val = out.strip().splitlines()[-1] if out.strip() else "?"
                print("  [0x%08x] %s" % (cell, val.strip()))
            mon.cmd("cont")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
