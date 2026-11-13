"""Boot the upstream Z62 board and take QMP screendumps.

Stage 21 is the graphics path: the LCD window at 0x20140000 and the GPU upload
window at 0x20180000. If the firmware really drives them, QMP screendump returns
a frame we can look at, so this walks the boot and dumps a PNG at fixed wall
times instead of trusting "it did not crash".

  python screen_probe.py --at 30,90,180,300 --out-dir run/screen
"""
import argparse
import json
import os
import socket
import subprocess
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = r"D:\z6ii_fw_lab\tools\msys64"
ROOT = r"D:\z6ii_fw_lab"
CMD = os.path.join(ROOT, r".local\z62-smoke\command.json")


def env():
    e = dict(os.environ)
    e["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                          os.path.join(MSYS, "usr", "bin"),
                          os.path.join(MSYS, "bin"), e.get("PATH", "")])
    return e


class Qmp:
    def __init__(self, port, deadline=30):
        end = time.time() + deadline
        while True:
            try:
                self.s = socket.create_connection(("127.0.0.1", port), 1)
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.3)
        self.f = self.s.makefile("rwb")
        self._read()                       # greeting
        self.cmd("qmp_capabilities")

    def _read(self):
        line = self.f.readline()
        if not line:
            raise RuntimeError("QMP closed")
        return json.loads(line)

    def cmd(self, name, **args):
        req = {"execute": name}
        if args:
            req["arguments"] = args
        self.f.write((json.dumps(req) + "\n").encode())
        self.f.flush()
        while True:
            msg = self._read()
            if "return" in msg or "error" in msg:
                return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="30,90,180,300")
    ap.add_argument("--out-dir", default=r"D:\z6ii_fw_lab\work\qemu_build\run\screen")
    ap.add_argument("--port", type=int, default=4475)
    ap.add_argument("--no-icount", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    cmd = [c.replace("tools/src/qemu/build/qemu-system-arm.exe", QEMU)
           for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if args.no_icount:
        i = cmd.index("-icount")
        del cmd[i:i + 2]
    cmd += ["-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % args.port]

    log = open(os.path.join(ROOT, r".local\z62-smoke\screen.txt"), "wb")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env(), cwd=ROOT)
    t0 = time.time()
    try:
        qmp = Qmp(args.port)
        for mark in [float(x) for x in args.at.split(",")]:
            while time.time() - t0 < mark:
                time.sleep(0.5)
            path = os.path.join(args.out_dir, "t%03d.ppm" % mark)
            r = qmp.cmd("screendump", filename=path)
            ok = os.path.exists(path)
            print("t=%3.0fs screendump %s -> %s" %
                  (time.time() - t0, path, "ok" if ok else r))
            if ok:
                print("   size=%d bytes" % os.path.getsize(path))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
