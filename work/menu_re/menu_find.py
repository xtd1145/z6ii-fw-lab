"""Find the runtime menu model by searching RAM for label pointers.

The PLAYBACK MENU labels live in the image's UI pool (e.g. "Playback folder" at
0x110aebe0), but nothing in the image points at them by absolute address and no
offset table matched, so the menu model is almost certainly built at runtime.
While the menu is open the item array must hold pointers to those labels, so
this boots the board, opens the menu, and searches guest RAM for them with the
gdbstub's qSearch packet (this build's monitor has no search command).

  python work/menu_re/menu_find.py --value 0x110aebe0
  python work/menu_re/menu_find.py --value 0x110aec12 --menu
"""
import argparse
import json
import os
import re
import socket
import subprocess
import time

ROOT = r"D:\z6ii_fw_lab"
QEMU = ROOT + r"\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = ROOT + r"\tools\msys64"
LAB_AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                      for n, a in ((17, 0x132c0000), (18, 0x132e0000),
                                   (32, 0x13e80000)))
SCAN = (0xC1800000, 0x00800000)     # app state / stack window


def env():
    e = dict(os.environ)
    e["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                          os.path.join(MSYS, "usr", "bin"),
                          os.path.join(MSYS, "bin"), e.get("PATH", "")])
    e["Z62_CLEAR_STATUS"] = "1"
    return e


def tcp(port, deadline=40):
    end = time.time() + deadline
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), 1)
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.3)


class Hmp:
    def __init__(self, port):
        self.s = tcp(port)
        self.s.settimeout(20)
        self.buf = b""
        self._prompt()

    def _prompt(self):
        while b"(qemu)" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("monitor closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"(qemu)")
        return out.decode("utf-8", "replace")

    def cmd(self, text):
        self.s.sendall((text + "\n").encode())
        return self._prompt()


class Qtest:
    def __init__(self, port):
        self.s = tcp(port)
        self.f = self.s.makefile("rwb")

    def cmd(self, text):
        self.f.write((text + "\n").encode())
        self.f.flush()
        while True:
            line = self.f.readline().decode().strip()
            if line.startswith(("OK", "FAIL")) or not line:
                return line


class Rsp:
    def __init__(self, port):
        self.s = tcp(port)
        self.s.settimeout(30)
        self.buf = b""
        self.s.sendall(b"+")

    def _recv(self):
        while True:
            if b"$" in self.buf:
                head, _, rest = self.buf.partition(b"$")
                if b"#" in rest:
                    body, _, tail = rest.partition(b"#")
                    self.buf = tail[2:]
                    return body.decode("ascii", "replace")
            data = self.s.recv(65536)
            if not data:
                raise RuntimeError("gdb closed")
            self.buf += data

    def cmd(self, body):
        payload = body.encode()
        self.s.sendall(b"$" + payload + b"#%02x" % (sum(payload) & 0xFF))
        reply = self._recv()
        self.s.sendall(b"+")
        return reply


def rsp_resume(rsp):
    payload = b"c"
    rsp.s.sendall(b"$" + payload + b"#%02x" % (sum(payload) & 0xFF))


def rsp_interrupt(rsp):
    """Stop the guest so memory can be read; the stub answers with Txx."""
    rsp.s.sendall(b"")
    try:
        rsp._recv()
    except socket.timeout:
        pass


def guest_seconds(rsp):
    reply = rsp.cmd("m4266008,8")
    if not reply.startswith("m"):
        return 0.0
    return int.from_bytes(bytes.fromhex(reply[1:]), "little") / 48000000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--value", default="0x110aebe0")
    ap.add_argument("--no-menu", action="store_true")
    ap.add_argument("--port-base", type=int, default=4610)
    args = ap.parse_args()
    want = int(args.value, 16)
    le = want.to_bytes(4, "little").hex()

    gdb_port = args.port_base
    wire_port = args.port_base + 1
    hmp_port = args.port_base + 2
    # Reuse the console's own command line: it carries the full device set the
    # board needs (controller I2C banks, loaders, aux groups). Only the
    # channels are replaced so this run can own them.
    base = json.load(open(os.path.join(ROOT, ".local", "webui", "run",
                                       "last-command.json")))
    cmd = []
    skip = False
    for i, part in enumerate(base):
        if skip:
            skip = False
            continue
        if part in ("-qmp", "-qtest", "-monitor", "-D", "-d"):
            skip = True
            continue
        cmd.append(part)
    cmd += ["-gdb", "tcp:127.0.0.1:%d" % gdb_port,
            "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % wire_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = sorted(re.findall(r"(device\[\d+\]) \(child<nikon-z72-controller-gpio>\)",
                                  listing), key=lambda n: int(re.search(r"\d+", n).group()))
        configs = sorted(re.findall(r"(device\[\d+\]) \(child<nikon-z72-boot-config>\)",
                                    listing), key=lambda n: int(re.search(r"\d+", n).group()))
        print("gpio banks:", len(gpios), "boot configs:", len(configs))
        wire = Qtest(wire_port)
        for bank, pin, level in ((2, 5, 0), (3, 13, 1), (4, 9, 1), (6, 7, 1),
                                 (1, 11, 1), (6, 14, 1), (6, 15, 1)):
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        if configs:
            for level in (0, 1):
                wire.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                         % (configs[0], level))
        rsp = Rsp(gdb_port)
        rsp.cmd("?")           # sync; the stub reports its stop
        rsp_resume(rsp)        # connecting leaves the vCPUs stopped
        t0 = time.time()
        g = 0.0
        while time.time() - t0 < 240:
            time.sleep(3)
            rsp_interrupt(rsp)
            g = guest_seconds(rsp)
            print("  guest=%.2f" % g, flush=True)
            rsp_resume(rsp)
            if g >= 2.4:
                break
        print("guest:", round(g, 2))
        if not args.no_menu:
            wire.cmd("set_irq_in /machine/peripheral/keys pins 9 0")
            time.sleep(2.5)
            wire.cmd("set_irq_in /machine/peripheral/keys pins 9 1")
            time.sleep(16)
            print("menu keyed; guest:", round(guest_seconds(rsp), 2))
        rsp_interrupt(rsp)
        addr, length = SCAN
        reply = rsp.cmd("qSearch:memory:%x;%x;%s" % (addr, length, le))
        if reply.startswith("1,"):
            hits = [int(x, 16) for x in reply[2:].split(",") if x]
            print("找到 %d 处指向 0x%08x 的指针:" % (len(hits), want))
            for h in hits[:24]:
                print("   0x%08x" % h)
        else:
            print("未找到; 回复:", reply[:80])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
