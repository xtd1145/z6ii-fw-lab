"""Ask whether the firmware ever reads its physical capture buffer.

The scatter record 10034070 names PA 80800000 / 0x2313880 as the master
engine's capture buffer, and z62_sensor_sim_fill_capture() now puts a real
Bayer frame there. Whether the CaptureService reads it is a separate question,
and the only honest way to answer it is a hardware read watchpoint on the
region while a MENU-triggered capture runs.

QEMU's gdbstub implements Z4 (access watchpoint), which this uses. A hit pauses
the CPU; we log the PC and the address, then resume. Boot needs the controller
GPIO inputs asserted, so qtest runs alongside.

  python cap_read_probe.py --press 45 --stop 75
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = r"D:\z6ii_fw_lab\tools\msys64"
ROOT = r"D:\z6ii_fw_lab"
CMD = os.path.join(ROOT, r".local\z62-smoke\command.json")

GPIO_INPUTS = ((2, 5, 0), (3, 13, 1), (4, 9, 1), (6, 7, 1),
               (1, 11, 1), (6, 14, 1), (6, 15, 1))
MENU_PIN = 9
CAPTURE_PA = 0x80800000
CAPTURE_LEN = 0x1000          # watch one page is enough to catch a read


def env():
    e = dict(os.environ)
    e["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                          os.path.join(MSYS, "usr", "bin"),
                          os.path.join(MSYS, "bin"), e.get("PATH", "")])
    return e


def tcp(port, deadline=40):
    end = time.time() + deadline
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", port), 1)
            s.settimeout(15)
            return s
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.3)


class Hmp:
    def __init__(self, port):
        self.s = tcp(port)
        self.buf = b""
        self._prompt()

    def _prompt(self):
        while b"(qemu)" not in self.buf:
            chunk = self.s.recv(1 << 16)
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
            line = self.f.readline().decode("utf-8", "replace").strip()
            if line.startswith(("OK", "FAIL")) or not line:
                return line


class Rsp:
    """Minimal gdb remote serial protocol client (no ack mode)."""

    def __init__(self, port):
        self.s = tcp(port)
        self.buf = b""

    def _read_packet(self, timeout):
        self.s.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            # Acks and interrupt bytes can precede a packet; a buffer that
            # starts with '+' would otherwise never match and every reply
            # would look like a timeout.
            while self.buf[:1] in (b"+", b"-", b"\x03"):
                self.buf = self.buf[1:]
            if self.buf.startswith(b"$"):
                i = self.buf.find(b"#")
                if i >= 0 and len(self.buf) >= i + 3:
                    body = self.buf[1:i]
                    self.buf = self.buf[i + 3:]
                    return body.decode("latin1")
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk
        return None

    def cmd(self, text, timeout=3.0):
        payload = text.encode()
        csum = sum(payload) & 0xFF
        self.s.sendall(b"$" + payload + b"#%02x" % csum)
        return self._read_packet(timeout)

    def send_only(self, text):
        payload = text.encode()
        csum = sum(payload) & 0xFF
        self.s.sendall(b"$" + payload + b"#%02x" % csum)

    def regs(self):
        """Return r0..r15 + cpsr as a list of ints (best effort)."""
        g = self.cmd("g")
        out = []
        if not g:
            return out
        g = g.strip()
        for i in range(0, min(len(g), 17 * 8), 8):
            word = g[i:i + 8]
            if len(word) < 8:
                break
            try:
                out.append(int.from_bytes(bytes.fromhex(word), "little"))
            except ValueError:
                out.append(0)
        return out

    def read_mem(self, addr, length):
        r = self.cmd("m%x,%x" % (addr, length))
        if not r:
            return b""
        r = r.strip()
        if r.startswith("E"):
            return b""
        try:
            return bytes.fromhex(r)
        except ValueError:
            return b""

    @staticmethod
    def _thumb_clear(addr):
        return (addr & ~1) if addr else addr

    def dump_frame(self, label, stack_words=16):
        """Log PC/LR/SP/argument registers and scan the stack for code-pointers.

        A breakpoint hit on a service routine is only actionable with its caller:
        the recorded LR and any return-address-looking words on the stack are
        what let us trace back to whoever registered or invoked the routine.
        """
        regs = self.regs()
        if len(regs) < 16:
            print("  %s (registers unavailable)" % label)
            return
        pc, lr, sp = regs[15], regs[14], regs[13]
        print("  %s pc=%08x lr=%08x->%08x sp=%08x" %
              (label, pc, lr, self._thumb_clear(lr), sp))
        print("  r0=%08x r1=%08x r2=%08x r3=%08x r12=%08x" %
              tuple(regs[i] for i in (0, 1, 2, 3, 12)))
        stack = self.read_mem(sp, stack_words * 4)
        cands = []
        for i in range(0, len(stack) - 3, 4):
            v = int.from_bytes(stack[i:i + 4], "little")
            # Master firmware code window; thumb bit cleared on display.
            if 0x10000000 <= v <= 0x13000000:
                cands.append((sp + i, self._thumb_clear(v)))
        for addr, v in cands[:12]:
            print("    stack[%08x] -> %08x" % (addr, v))
        if not cands:
            print("    (no code-looking words on stack)")


def instances(listing, kind):
    names = re.findall(r"(device\[\d+\]) \(child<" + kind + r">\)", listing)
    return sorted(names, key=lambda n: int(re.search(r"\d+", n).group()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--press", type=float, default=45.0)
    ap.add_argument("--stop", type=float, default=75.0)
    ap.add_argument("--port-base", type=int, default=4530)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=CAPTURE_PA,
                    help="address to watch (default: capture buffer)")
    ap.add_argument("--aux", default="", help="master boot aux parameter groups")
    ap.add_argument("--extra", default="", help="comma separated -global props")
    ap.add_argument("--watch-len", type=lambda x: int(x, 0), default=4,
                    help="QEMU only accepts small watch lengths; 4 catches a "
                         "word-sized read of the buffer head")
    args = ap.parse_args()

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if args.aux:
        for i, part in enumerate(cmd):
            if "master-boot-aux=" in part:
                cmd[i] = re.sub(r"master-boot-aux=.*?(?=,controller-flash-data=)",
                                "master-boot-aux=" + args.aux, part)
                break
    for drop in ("-monitor",):
        if drop in cmd:
            i = cmd.index(drop)
            del cmd[i:i + 2]
    for prop in (
        "nikon-z72-boot-config.frame-start=on",
        "nikon-z72-boot-config.laboratory-capseq-trace=on",
        "nikon-z72-boot-config.laboratory-capseq-actuator=on",
        "nikon-z72-boot-config.capseq-delay-ns=200000",
    ):
        cmd += ["-global", prop]
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]
    qtest_port = args.port_base
    hmp_port = args.port_base + 7
    gdb_port = args.port_base + 8
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port,
            "-gdb", "tcp:127.0.0.1:%d,server=on,wait=off" % gdb_port]

    log = open(os.path.join(ROOT, r".local\z62-smoke\capread.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env())
    t0 = time.time()
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = instances(listing, "nikon-z72-controller-gpio")
        wire = Qtest(qtest_port)
        for bank, pin, level in GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        print("gpio asserted; connecting gdb ...")

        rsp = Rsp(gdb_port)
        print("qSupported ->", (rsp.cmd("qSupported:multiprocess+") or "")[:60])
        rsp.send_only("QStartNoAckMode")
        # Watch the physical capture buffer for any access.
        r = rsp.cmd("Z4,%x,%x" % (args.addr, args.watch_len))
        print("Z4 watch 0x%08x len 0x%x -> %r" % (args.addr, args.watch_len, r))
        rsp.send_only("c")

        pressed = False
        hits = []
        reads = 0
        while time.time() - t0 < args.stop:
            now = time.time() - t0
            if not pressed and now >= args.press:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0"
                         % MENU_PIN)
                print("t=%3.0fs MENU press" % now)
                time.sleep(0.25)
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1"
                         % MENU_PIN)
                print("t=%3.0fs MENU release" % now)
                pressed = True
            pkt = rsp._read_packet(0.4)
            if pkt is None:
                continue
            if pkt.startswith(("T", "S")):
                # stop reply: watchpoint hit. Read the pc and the watch value.
                g = rsp.cmd("g")
                pc = None
                if g and len(g) >= 16 * 8:
                    try:
                        pc = int.from_bytes(bytes.fromhex(g[15 * 8:16 * 8]),
                                            "little")
                    except ValueError:
                        pc = None
                hits.append(pc)
                reads += 1
                if len(hits) <= 12:
                    print("  watch hit #%d pc=%s" %
                          (len(hits), "0x%08x" % pc if pc else "?"))
                    rsp.dump_frame("callframe")
                rsp.send_only("c")
        print("\n=== 结果 ===")
        print("watch hits on 0x%08x: %d" % (args.addr, len(hits)))
        if hits:
            from collections import Counter
            for pc, n in Counter(hits).most_common(10):
                print("  pc=%s x%d" % ("0x%08x" % pc if pc else "?", n))
        else:
            print("固件在整个窗口内未访问 0x%08x" % args.addr)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
