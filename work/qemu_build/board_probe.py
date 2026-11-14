"""Boot the upstream Z62 board, assert its power inputs, drive keys, dump screens.

The board stays in standby until the controller GPIO banks see the power and run
inputs, so a plain start gives a black 1024x680 surface. This repeats what
tools/z62_probe.py:initialize_inputs does -- enumerate the unattached devices
through HMP, drive the eight controller GPIO banks over qtest, enable frame sync
on both engines -- then takes QMP screendumps and can click MENU on the key
expander (pin 9) while the shooting foreground is up.

  python board_probe.py --sd D:/z6ii_fw_lab/.local/sdcard.raw \
      --at 55,80,110 --menu-start 48 --menu-every 10
"""
import argparse
import json
import os
import re
import socket
import subprocess
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = r"D:\z6ii_fw_lab\tools\msys64"
ROOT = r"D:\z6ii_fw_lab"
CMD = os.path.join(ROOT, r".local\z62-smoke\command.json")

GPIO_INPUTS = ((2, 5, 0), (3, 13, 1), (4, 9, 1), (6, 7, 1),
               (1, 11, 1), (6, 14, 1), (6, 15, 1))
MENU_PIN = 9
ZERO = bytes(3)


def env():
    e = dict(os.environ)
    e["PATH"] = ";".join([os.path.join(MSYS, "ucrt64", "bin"),
                          os.path.join(MSYS, "usr", "bin"),
                          os.path.join(MSYS, "bin"), e.get("PATH", "")])
    return e


def tcp(port, deadline=30):
    end = time.time() + deadline
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", port), 1)
            s.settimeout(10)
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
            line = self.f.readline().decode("utf-8", "replace").strip()
            if line.startswith(("OK", "FAIL")) or not line:
                return line


class Qmp:
    def __init__(self, port):
        self.s = tcp(port)
        self.f = self.s.makefile("rwb")
        self.f.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, name, **args):
        req = {"execute": name}
        if args:
            req["arguments"] = args
        self.f.write((json.dumps(req) + "\n").encode())
        self.f.flush()
        while True:
            msg = json.loads(self.f.readline())
            if "return" in msg or "error" in msg:
                return msg


def instances(listing, kind):
    names = re.findall(r"(device\[\d+\]) \(child<" + kind + r">\)", listing)
    return sorted(names, key=lambda n: int(re.search(r"\d+", n).group()))


def nonblack(path, stride=499):
    data = open(path, "rb").read()
    body = data.split(b"\n", 3)[-1]
    return sum(1 for i in range(0, len(body), 3 * stride)
               if body[i:i + 3] != ZERO)


def strip_globals(cmd, prefix):
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == "-global" and i + 1 < len(cmd) and \
           cmd[i + 1].startswith(prefix):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def override_adc(cmd, values):
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == "-global" and i + 1 < len(cmd) and \
           cmd[i + 1].startswith("nikon-z72-controller-adc.channel"):
            ch = cmd[i + 1].split("channel", 1)[1].split("=")[0]
            idx = {"0": 0, "1": 1, "3": 2}.get(ch)
            if idx is not None:
                out += ["-global", "nikon-z72-controller-adc.channel%s=%s"
                        % (ch, values[idx])]
                i += 2
                continue
        out.append(cmd[i])
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="60,180,300")
    ap.add_argument("--out-dir", default=r"D:\z6ii_fw_lab\work\qemu_build\run\screen")
    ap.add_argument("--port-base", type=int, default=4480)
    ap.add_argument("--main-screen", action="store_true")
    ap.add_argument("--no-adc", action="store_true")
    ap.add_argument("--adc", default="", help="override channel0,1,3 values")
    ap.add_argument("--extra", default="", help="comma separated -global props")
    ap.add_argument("--master", default="",
                    help="override master-boot-image= with another MAIN "
                         "module (e.g. a patched combo build)")
    ap.add_argument("--sd", default="", help="raw SD image to attach")
    ap.add_argument("--no-cpu-loader", action="store_true",
                    help="drop -device loader,addr=... entries so the reset PC "
                         "comes from the boot store (native NOR boot)")
    ap.add_argument("--no-preload", action="store_true",
                    help="strip boot image/aux preloads from -M (native NOR boot)")
    ap.add_argument("--machine-extra", default="",
                    help="text appended to the -M machine string")
    ap.add_argument("--nor", default="",
                    help="raw parallel-NOR dump for pflash (native-nor-boot)")
    ap.add_argument("--sd-write", action="store_true",
                    help="attach the SD image without snapshot=on, so guest "
                         "writes reach the file (needed to prove card writes)")
    ap.add_argument("--no-frame-sync", action="store_true")
    ap.add_argument("--menu-start", type=float, default=0.0,
                    help="start clicking MENU at this wall time")
    ap.add_argument("--menu-every", type=float, default=10.0,
                    help="seconds between MENU clicks; 0 clicks once")
    ap.add_argument("--no-uart", action="store_true")
    ap.add_argument("--aux", default="",
                    help="override master-boot-aux= with group@addr list")
    ap.add_argument("--sync-at", default="", help="times to pulse sync-event")
    ap.add_argument("--sync-event", type=int, default=15)
    ap.add_argument("--console", default=".local/z62-smoke/console.log")
    ap.add_argument("--dump", default="", help="comma separated addresses to read at each shot")
    ap.add_argument("--menu-hold", type=float, default=0.2,
                    help="seconds the MENU key stays held")
    ap.add_argument("--log", default="", help="QEMU -d categories")
    ap.add_argument("--dfilter", default="",
                    help="QEMU -dfilter range (limits -d exec to given PCs)")
    ap.add_argument("--pc", action="store_true", help="dump every core's PC at each shot")
    ap.add_argument("--trace", default="", help="QEMU -D log file")
    ap.add_argument("--icount-shift", type=int, default=None,
                    help="override -icount shift=N (0 = 1 ns/insn). The model "
                         "runs far faster than silicon on hardware delays; "
                         "raising this scales every guest-visible interval, so "
                         "it is a probe knob, not a calibration.")
    args = ap.parse_args()
    args.out_dir = os.path.abspath(args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)

    cmd = [c for c in json.load(open(CMD))]
    cmd[0] = QEMU
    if args.no_cpu_loader:
        out = []
        i = 0
        while i < len(cmd):
            if cmd[i] == "-device" and i + 1 < len(cmd) and \
                    cmd[i + 1].startswith("loader,addr="):
                i += 2
                continue
            out.append(cmd[i])
            i += 1
        cmd = out
    if args.no_preload:
        cmd[2] = re.sub(r",master-boot-aux=.*?(?=,controller-flash-data=)",
                        "", cmd[2])
        cmd[2] = re.sub(r",(master|slave)-boot-image=[^,]*", "", cmd[2])
        cmd[2] = re.sub(r",+", ",", cmd[2])
    if args.machine_extra:
        cmd[2] = cmd[2] + args.machine_extra
    if args.master:
        for i, part in enumerate(cmd):
            if "master-boot-image=" in part:
                cmd[i] = re.sub(r"master-boot-image=[^,]*",
                                "master-boot-image=" + args.master, part)
                break
    if args.aux:
        for i, part in enumerate(cmd):
            if "master-boot-aux=" in part:
                cmd[i] = re.sub(r"master-boot-aux=.*?(?=,controller-flash-data=)",
                                "master-boot-aux=" + args.aux, part)
                break
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    if args.icount_shift is not None and "-icount" in cmd:
        i = cmd.index("-icount") + 1
        cmd[i] = "shift=%d,sleep=off" % args.icount_shift
    if args.no_adc:
        cmd = strip_globals(cmd, "nikon-z72-controller-adc.")
    if args.no_frame_sync:
        cmd = strip_globals(cmd, "nikon-z72-boot-config.frame-sync")
    if args.adc:
        cmd = override_adc(cmd, args.adc.split(","))
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]
    if args.sd:
        snap = "" if args.sd_write else ",snapshot=on"
        cmd += ["-global", "nikon-z72-boot-config.sd-host=on",
                "-drive", "if=none,id=z62-card,file=%s,format=raw%s"
                          % (os.path.abspath(args.sd), snap),
                "-device", "sd-card,drive=z62-card,bus=/z72-sd-slave/sd-bus"]

    if args.nor:
        cmd += ["-drive", "if=pflash,format=raw,snapshot=on,file=%s"
                          % os.path.abspath(args.nor)]
    if not args.no_uart:
        cmd += ["-chardev", "file,id=z72-console,path=" + os.path.abspath(os.path.join(ROOT, args.console)),
                "-global", "nikon-z72-uart.enabled=on"]

    qtest_port = args.port_base
    qmp_port = args.port_base + 1
    hmp_port = args.port_base + 7
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]

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

    if args.dfilter:
        cmd += ["-dfilter", args.dfilter]
    if args.log:
        cmd += ["-d", args.log]
        cmd += ["-D", os.path.abspath(args.trace)]
    logp = os.path.join(ROOT, r".local\z62-smoke\board.txt")
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env(), cwd=ROOT)
    t0 = time.time()
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = instances(listing, "nikon-z72-controller-gpio")
        configs = instances(listing, "nikon-z72-boot-config")
        print("gpio banks:", len(gpios), "boot configs:", len(configs))
        if len(gpios) != 8:
            raise SystemExit("expected eight GPIO banks, got %d" % len(gpios))
        wire = Qtest(qtest_port)
        for bank, pin, level in GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        print("  power/run inputs asserted")
        if len(configs) == 2 and not args.no_frame_sync:
            for level in (0, 1):
                wire.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                         % (configs[0], level))
            print("  frame-sync toggled")

        qmp = Qmp(qmp_port)
        sync_times = sorted(float(x) for x in args.sync_at.split(",") if x)
        shots = sorted(float(x) for x in args.at.split(",") if x)
        next_menu = args.menu_start if args.menu_start > 0 else None
        release_at = None
        while shots or next_menu is not None or release_at is not None                 or sync_times:
            now = time.time() - t0
            shot_due = shots[0] if shots else 1e9
            sync_due = sync_times[0] if sync_times else 1e9
            menu_due = next_menu if next_menu is not None else 1e9
            rel_due = release_at if release_at is not None else 1e9
            due = min(shot_due, menu_due, rel_due, sync_due)
            if due > now:
                time.sleep(min(0.4, max(0.05, due - now)))
                continue
            if sync_due <= now:
                sync_times.pop(0)
                for level in (1, 0):
                    wire.cmd("set_irq_in /machine/unattached/%s sync-event %d %d"
                             % (configs[0], args.sync_event, level))
                print("t=%3.0fs sync-event %d pulse" % (now, args.sync_event))
                continue
            if rel_due <= now:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1" % MENU_PIN)
                print("t=%3.0fs MENU release" % now)
                release_at = None
                continue
            if menu_due <= now:
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0" % MENU_PIN)
                print("t=%3.0fs MENU press (hold %.1fs)" % (now, args.menu_hold))
                release_at = menu_due + args.menu_hold
                next_menu = (menu_due + args.menu_every
                             if args.menu_every > 0 else None)
                continue
            mark = shots.pop(0)
            if args.pc:
                pcs = []
                for cpu in range(4):
                    hmp.cmd("cpu %d" % cpu)
                    regs = hmp.cmd("info registers")
                    m = re.search(r"R15=([0-9a-f]{8})", regs)
                    pcs.append("cpu%d=0x%s" % (cpu, m.group(1) if m else "?"))
                hmp.cmd("cpu 0")
                print("   PC: " + "  ".join(pcs))
            for name in ("successful-uploads", "render-requests",
                         "rendered-commands", "unsupported-commands",
                         "failed-requests"):
                out = hmp.cmd("qom-get /machine/master-graphics " + name)
                print("   gpu %s = %s" % (name, out.strip().splitlines()[-1]))
            for cfg in configs:
                for name in ("idmac-kicks", "idmac-reads",
                             "capseq-requests", "capseq-completions"):
                    out = hmp.cmd("qom-get /machine/unattached/%s %s"
                                  % (cfg, name))
                    print("   %s %s = %s" % (cfg, name,
                                             out.strip().splitlines()[-1]))
            for a in [x for x in args.dump.split(",") if x]:
                print("   " + a)
                print(hmp.cmd("xp/16xw %s" % a).strip())
            path = os.path.join(args.out_dir, "b%03d.ppm" % int(mark))
            qmp.cmd("screendump", filename=path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            print("t=%3.0fs %s size=%d nonblack=%d" %
                  (now, path, size, nonblack(path) if size else 0))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
