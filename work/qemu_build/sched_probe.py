"""Watch the RTOS ready-queues of both engines while driving a key event.

Both engines park in an idle loop that polls a per-core ready-queue word:

  mas  queue 0xc2b2a944 + core*4   idle PC 0x10107094
  sla  queue 0xc189f6c0 + core*4   idle PC 0x10038a68

With no host event the words stay zero forever, which is what the earlier
single-engine runs saw. This probe asserts the board power inputs, lets the
foreground come up, then clicks MENU on the key expander and samples both
queues (plus every core's PC) before and after, so we can tell whether a real
evdev-style key event is what makes the firmware create and dispatch a task.

  python sched_probe.py --at 45 --press 50 --after 58
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
KEY_PINS = {9: "MENU"}

MAS_QUEUE = 0xC2B2A944
SLA_QUEUE = 0xC189F6C0


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


def instances(listing, kind):
    names = re.findall(r"(device\[\d+\]) \(child<" + kind + r">\)", listing)
    return sorted(names, key=lambda n: int(re.search(r"\d+", n).group()))


WORD = re.compile(r"^[0-9a-f]{8}:\s+(.*)$")


def read_words(hmp, addr, count, raw=False, cpu=None):
    if cpu is not None:
        hmp.cmd("cpu %d" % cpu)
    # `x` walks the current CPU's MMU; `xp` is physical. The slave engine runs
    # each task in its own TTBR0 context (see the dispatcher at 0x10038a78),
    # so its globals must be read through the virtual mapping.
    prefix = "x" if cpu is not None else "xp"
    out = hmp.cmd("%s/%dxw 0x%x" % (prefix, count, addr))
    vals = []
    for line in out.splitlines():
        if "%s/" % prefix in line:      # echoed command, not data
            continue
        m = WORD.match(line.strip())
        if not m:
            continue
        vals += [int(x, 16) for x in re.findall(r"0x([0-9a-f]{8})\b", m.group(1))]
    if raw:
        print("   [raw %s%x] %s" % (prefix, addr, out.replace("\n", " | ")))
    return vals[:count]


def read_pcs(hmp, smp):
    pcs = {}
    for cpu in range(smp):
        hmp.cmd("cpu %d" % cpu)
        regs = hmp.cmd("info registers")
        m = re.search(r"R15=([0-9a-f]{8})", regs)
        pcs[cpu] = int(m.group(1), 16) if m else None
    hmp.cmd("cpu 0")
    return pcs


def sample(hmp, smp, label, t, raw=False):
    mas = read_words(hmp, MAS_QUEUE, 4, raw, cpu=0)
    sla = read_words(hmp, SLA_QUEUE, 8, raw, cpu=2)
    sla_p = read_words(hmp, SLA_QUEUE, 8)
    pcs = read_pcs(hmp, smp)
    print("t=%3.0fs %-14s mas_q=%s" % (t, label, [hex(v) for v in mas]))
    print("        sla_q virt=%s" % [hex(v) for v in sla[:4]])
    print("        sla_q phys=%s" % [hex(v) for v in sla_p[:4]])
    print("        PCs: " + "  ".join(
        "cpu%d=0x%08x" % (c, p) if p is not None else "cpu%d=?" % c
        for c, p in pcs.items()))
    for name, q, cpu in (("mas", mas, 0), ("sla", sla, 2)):
        for slot, tcb in enumerate(q):
            if tcb and tcb > 0x1000:
                words = read_words(hmp, tcb, 0xd0 >> 2, cpu=cpu)
                if len(words) < 0x32:
                    print("        %s slot%d tcb=0x%08x (read short)"
                          % (name, slot, tcb))
                    break
                print("        %s slot%d tcb=0x%08x"
                      % (name, slot, tcb))
                print("          +0x00=%08x +0x04=%08x +0x08=%08x +0x0c=%08x"
                      % tuple(words[0:4]))
                print("          +0x10=%08x +0x14=%08x +0x18=%08x +0x1c=%08x"
                      % tuple(words[4:8]))
                print("          +0x24=%08x  +0xbc(sp)=%08x +0xc0(ttbr0)=%08x "
                      "+0xc4(asid)=%08x"
                      % (words[0x24 >> 2], words[0xbc >> 2],
                         words[0xc0 >> 2], words[0xc4 >> 2]))
                break
    return mas, sla, pcs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=45.0,
                    help="first queue sample time")
    ap.add_argument("--press", type=float, default=50.0,
                    help="MENU press time")
    ap.add_argument("--hold", type=float, default=0.25)
    ap.add_argument("--after", type=float, default=58.0,
                    help="post-press sample time")
    ap.add_argument("--smp", type=int, default=4)
    ap.add_argument("--port-base", type=int, default=4500)
    ap.add_argument("--main-screen", action="store_true")
    args = ap.parse_args()

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    if args.main_screen:
        for prop in (
            "nikon-z72-boot-config.frame-start=on",
            "nikon-z72-input-config.laboratory-metadata-completion=on",
            "nikon-z72-boot-config.input-command-completion=on",
            "nikon-z62-sensor-config.laboratory-mode-completion=on",
            "nikon-z62-sensor-config.laboratory-frame-completion=on",
            "nikon-z62-sensor-config.laboratory-init-completion=on",
            "nikon-z62-sensor-config.init-delay-ns=250000000",
        ):
            cmd += ["-global", prop]
    qtest_port = args.port_base
    hmp_port = args.port_base + 7
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]

    log = open(os.path.join(ROOT, r".local\z62-smoke\sched.txt"), "wb")
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
        print("power/run inputs asserted; gpio banks=%d" % len(gpios))

        schedule = [("boot", args.at), ("pressed", args.press),
                    ("after", args.after)]
        schedule.sort(key=lambda kv: kv[1])
        released = False
        samples = {}
        while schedule:
            now = time.time() - t0
            label, due = schedule[0]
            if now < due:
                time.sleep(min(0.25, max(0.02, due - now)))
                continue
            schedule.pop(0)
            if label == "pressed":
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0"
                         % MENU_PIN)
                print("t=%3.0fs MENU press (pin %d)" % (now, MENU_PIN))
                continue
            if label == "after" and not released:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1"
                         % MENU_PIN)
                print("t=%3.0fs MENU release" % now)
                released = True
            samples[label] = sample(hmp, args.smp, label, now,
                                    raw=(label == "boot"))
        if not released:
            wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1" % MENU_PIN)

        print("\n=== 判定 ===")
        mb, sb, _ = samples["boot"]
        ma, sa, pa = samples["after"]
        print("mas 队列 boot=%s after=%s" % (mb, ma))
        print("sla 队列 boot=%s after=%s" % (sb, sa))
        if any(ma) or any(sa):
            print("结论：按键事件触发了任务创建")
        else:
            print("结论：按键未进入该路径（队列仍空）")
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
