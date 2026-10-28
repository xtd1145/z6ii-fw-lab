#!/usr/bin/env python3
"""Experimental web console for the Z6 II QEMU lab: swap firmware in, boot, watch.

Why this exists instead of tools/z62_console.py: that console pins the three
engine images to the C1.70 SHA-256 identities, so it refuses anything patched or
re-packed. The QEMU machine itself does not check identities, so this console
keeps the checks as a warning badge and lets a new image boot. That is the whole
point: try a re-signed build without editing scripts first.

Stdlib only, listens on 127.0.0.1, one machine at a time.

  python z62_webui/console.py --port 8642
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / ".local" / "webui"
INPUTS = WEBUI / "inputs"
RUN = WEBUI / "run"
QEMU = ROOT / "tools" / "src" / "qemu" / "build" / "qemu-system-arm.exe"
MSYS = ROOT / "tools" / "msys64"

# Known-good C1.70 identities: reported, never enforced.
IDENTITIES = {
    "master": (38750138, "bb83613121f843d65101da936d942acdc513d86dbd3abc26259d0d870d03f5d4"),
    "slave": (8832194, "3fd8c2cbdf57fadb03f6d69b596b58a8703efdd61ae82572d5afeea0241a9ed5"),
    "controller": (524290, "9c65cad554ed02f83d37fbc825a8eb9efe79e3f32f98894dd5dd73edee416983"),
}
GROUPS = ((17, 0x132c0000, 0x20000), (18, 0x132e0000, 0xa0000),
          (32, 0x13e80000, 0x180000))
NOR_SIZE = 0x4000000
FOOTER = b"ENDM"
# Completed firmware work in this workspace lives under dist/: equal-length
# patched master modules (loadable straight into the emulator) and the re-signed
# update containers that carry them.
DIST = ROOT / "dist"
STOCK_MASTER = ROOT / "fw_verified" / "eg1850_mas_01700"
KEYJSON = ROOT / "key.json"
MASTER_SIZE = 38750138
CONTAINER_SIZE = 49002888
MAS_BASE = 0x10020000
CACHE = WEBUI / "variants.json"
SELECTIONS = WEBUI / "selections.json"


def load_selections():
    try:
        return json.loads(SELECTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


SLOT_SIZES = {"master": 38750138, "slave": 8832194, "controller": 524290}


def remember(slot, path):
    """Pin a slot to a file so a page refresh cannot silently revert to stock.

    Without this the tool would happily boot the original master while the
    operator believes the patched one is running.
    """
    expected = SLOT_SIZES.get(slot)
    if expected:
        size = Path(path).stat().st_size if Path(path).is_file() else 0
        if abs(size - expected) > max(1024, expected // 50):
            raise ValueError(
                "%s 期望约 %d 字节，实际 %d；这不像该槽位的镜像，拒绝固定"
                % (slot, expected, size))
    picked = load_selections()
    picked[slot] = str(Path(path))
    SELECTIONS.parent.mkdir(parents=True, exist_ok=True)
    SELECTIONS.write_text(json.dumps(picked, indent=1), encoding="utf-8")
    return picked


def safe_pin(slot, path):
    """remember() but reported to the caller instead of killing the handler."""
    try:
        return dict(pinned=remember(slot, path)), None
    except ValueError as error:
        return None, str(error)


def forget(slot):
    picked = load_selections()
    picked.pop(slot, None)
    SELECTIONS.write_text(json.dumps(picked, indent=1), encoding="utf-8")
    return picked
KEYS = {"menu": 9, "ok": 3, "up": 5, "down": 4, "left": 7, "right": 6}
POWER_INPUTS = ((2, 5, 0), (3, 13, 1), (4, 9, 1), (6, 7, 1),
                (1, 11, 1), (6, 14, 1), (6, 15, 1))
ADC_CHANNELS = ((0, 1800), (1, 3001), (3, 3001))


def qemu_env():
    e = dict(os.environ)
    e["PATH"] = ";".join([str(MSYS / "ucrt64" / "bin"), str(MSYS / "usr" / "bin"),
                          str(MSYS / "bin"), e.get("PATH", "")])
    return e


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def describe(path):
    p = Path(path)
    if not p.is_file():
        return dict(path=str(p), present=False)
    return dict(path=str(p), present=True, size=p.stat().st_size,
                sha256=digest(p))


def classify(slot, info):
    known = IDENTITIES.get(slot)
    if not info.get("present"):
        return "missing"
    if known and (info["size"], info["sha256"]) == known:
        return "stock C1.70"
    if known and info["size"] == known[0]:
        return "same size, different bytes (patched?)"
    if known is None:
        return "无原厂基准可比"
    return "non-stock"


def extract_groups(nor_path, dest):
    """Validate a NOR dump and write the three parameter groups."""
    nor = Path(nor_path)
    report = dict(source=str(nor), size=nor.stat().st_size if nor.exists() else 0,
                  groups=[], ok=False)
    if report["size"] != NOR_SIZE:
        report["error"] = "expected %d bytes, got %d" % (NOR_SIZE, report["size"])
        return report
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with nor.open("rb") as stream:
        for number, address, size in GROUPS:
            stream.seek(address - 0x10000000)
            data = stream.read(size)
            good = len(data) == size and data[-4:] == FOOTER
            if good:
                (dest / ("group%d.bin" % number)).write_bytes(data)
            report["groups"].append(dict(group=number, address=hex(address),
                                         accepted=good,
                                         sha256=hashlib.sha256(data).hexdigest()[:16]
                                         if data else None))
    report["ok"] = all(g["accepted"] for g in report["groups"])
    report["aux"] = ",,".join("%s/group%d.bin@%#x" % (dest, n, a)
                              for n, a, _ in GROUPS) if report["ok"] else None
    return report


def diff_runs(path, limit=10, chunk=1 << 16):
    """Byte-diff a patched master against the stock module.

    Only chunks that differ are scanned byte by byte, so an equal-length patch
    costs one pass over 38 MiB. Offsets are reported as guest virtual addresses
    because that is what a debugger or the emulator's monitor will show.
    """
    total, runs = 0, []
    stock = Path(STOCK_MASTER)
    if not stock.is_file():
        return None, []
    with open(path, "rb") as fa, open(stock, "rb") as fb:
        off = 0
        while True:
            ca, cb = fa.read(chunk), fb.read(chunk)
            if not ca:
                break
            if ca == cb:
                off += len(ca)
                continue
            i = 0
            while i < len(ca):
                if ca[i] != cb[i]:
                    j = i
                    while j < len(ca) and ca[j] != cb[j]:
                        j += 1
                    runs.append([MAS_BASE + off + i, j - i])
                    total += j - i
                    i = j
                else:
                    i += 1
            off += len(ca)
    return total, runs[:limit]


def variant_label(rel):
    text = rel.lower()
    for tag in ("beep", "noop", "probe", "signed", "decoy", "tv16000",
                "shutterbeep", "k8zero", "modcrc", "pkgcrc", "poc",
                "compressed"):
        if tag in text:
            return tag
    return "patched"


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def variants():
    """Inventory dist/ as bootable firmware variants, with cached diffs."""
    cache = load_cache()
    out = []
    if not DIST.is_dir():
        return out
    for path in sorted(DIST.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".bin", ".mas"):
            continue
        size = path.stat().st_size
        if size not in (MASTER_SIZE, CONTAINER_SIZE):
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        key = "%s:%d:%d" % (rel, size, int(path.stat().st_mtime))
        entry = cache.get(key)
        if entry is None:
            kind = "master" if size == MASTER_SIZE else "container"
            entry = dict(path=rel, kind=kind, size=size,
                         sha256=digest(path)[:16],
                         label=variant_label(rel))
            if kind == "master":
                total, runs = diff_runs(path)
                entry["diff_bytes"] = total
                entry["runs"] = [["0x%x" % a, n] for a, n in runs]
                entry["is_stock"] = total == 0
            cache[key] = entry
        out.append(entry)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return out


def extract_master(container):
    """Pull the mas module out of a re-signed update container."""
    src = Path(container)
    if not src.is_file():
        return dict(ok=False, error="container not found")
    if not KEYJSON.is_file():
        return dict(ok=False, error="key.json missing; cannot decrypt container")
    sys.path.insert(0, str(ROOT))
    try:
        import z6ii_patch as zp
        raw, plain = zp.open_container(str(src), zp.load_key(str(KEYJSON)))
        body, mods = zp.parse(plain)
        name, off, length = zp.find(mods, "eg1850_mas")
        data = bytes(body[off:off + length])
    except Exception as error:
        return dict(ok=False, error="%s: %s" % (type(error).__name__, error))
    INPUTS.mkdir(parents=True, exist_ok=True)
    dest = INPUTS / ("master__from_%s.BIN" % re.sub(r"[^A-Za-z0-9._-]", "_",
                                                    src.stem))
    dest.write_bytes(data)
    total, runs = diff_runs(dest)
    info = describe(dest)
    info.update(ok=True, slot="master", module=name,
                verdict=classify("master", info), diff_bytes=total,
                runs=[["0x%x" % a, n] for a, n in runs])
    return info


class Channel:
    """Line-oriented client shared by the qtest and QMP sockets."""

    def __init__(self, port, greeting=False):
        self.s = socket.create_connection(("127.0.0.1", port), 5)
        self.f = self.s.makefile("rwb")
        self.lock = threading.Lock()
        if greeting:
            self.f.readline()
            self.send({"execute": "qmp_capabilities"})

    def send(self, request):
        with self.lock:
            self.f.write((json.dumps(request) + "\n").encode())
            self.f.flush()
            return json.loads(self.f.readline())

    def cmd(self, line, expect_prefix=("OK", "FAIL", "ERR")):
        with self.lock:
            self.f.write((line + "\n").encode())
            self.f.flush()
            while True:
                text = self.f.readline().decode("utf-8", "replace").strip()
                if text.startswith(expect_prefix) or not text:
                    return text

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Hmp:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), 5)
        self.s.settimeout(10)
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
        with self._lock:
            self.s.sendall((text + "\n").encode())
            return self._prompt()

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    _lock = threading.Lock()


class Machine:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.started = None
        self.hmp = None
        self.qtest = None
        self.qmp = None
        self.log_path = None
        self.profile = None
        self.log_stream = None
        self.effective = None
        self._shot_lock = threading.Lock()
        self.gpios = []
        self.error = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def build_args(self, cfg):
        master = cfg.get("master")
        slave = cfg.get("slave")
        controller = cfg.get("controller")
        card = cfg.get("card")
        aux = cfg.get("aux")
        profile = cfg.get("profile", "z62-dual")
        out = RUN / ("%s-%d" % (profile, int(time.time())))
        out.mkdir(parents=True, exist_ok=True)
        self.log_path = out / "qemu.log"

        if profile == "z6ii":
            cmd = [str(QEMU), "-M", "nikon-z6ii", "-cpu", "cortex-a15",
                   "-kernel", master, "-smp", "2", "-display", "none",
                   "-serial", "none", "-monitor", "none"]
            return cmd, dict(qtest=None, qmp=None, hmp=None), profile

        resident = out / "resident.bin"
        blob = bytearray(0xC000)
        blob[:4] = b"MAKE"
        resident.write_bytes(blob)
        primary = bytearray(256)
        primary[11] = 1
        (out / "primary.bin").write_bytes(primary)
        auxiliary = bytearray(256)
        auxiliary[9], auxiliary[12] = 8, 0xC9
        (out / "auxiliary.bin").write_bytes(auxiliary)

        machine = ("nikon-z62-dual-lab,controller-firmware=%s"
                   ",controller-link=on,controller-battery-peer=on"
                   ",master-boot-image=%s,slave-boot-image=%s"
                   ",controller-flash-data=%s" % (controller, master, slave, resident))
        if aux:
            machine += ",master-boot-aux=" + aux
        ports = dict(qtest=free_port(), qmp=free_port(), hmp=free_port())
        cmd = [str(QEMU), "-M", machine, "-smp", "4,maxcpus=5",
               "-display", "none", "-serial", "none", "-nic", "none",
               "-icount", "shift=0,sleep=off",
               "-global", "nikon-z72-boot-config.frame-sync=on",
               "-global", "nikon-z72-uart.enabled=on",
               "-chardev", "file,id=z62-console,path=%s" % (out / "guest-console.log")]
        if cfg.get("main_screen"):
            for prop in ("nikon-z72-boot-config.frame-start=on",
                         "nikon-z72-input-config.laboratory-metadata-completion=on",
                         "nikon-z72-boot-config.input-command-completion=on",
                         "nikon-z62-sensor-config.laboratory-mode-completion=on",
                         "nikon-z62-sensor-config.laboratory-frame-completion=on",
                         "nikon-z62-sensor-config.laboratory-init-completion=on",
                         "nikon-z62-sensor-config.init-delay-ns=250000000"):
                cmd += ["-global", prop]
        for bank, pin in ADC_CHANNELS:
            cmd += ["-global", "nikon-z72-controller-adc.channel%d=%d" % (bank, pin)]
        for device in (
            "nikon-z72-controller-register-bank,bus=controller-i2c0,address=79",
            "nikon-z72-controller-expander,bus=controller-i2c0,address=34,id=keys,"
            "interrupt-gpio=/machine/controller/keys-gpio,interrupt-pin=8",
            "nikon-z72-controller-register-bank,bus=controller-i2c0,address=107,"
            "initial-state=%s" % (out / "primary.bin"),
            "nikon-z72-controller-register-bank,bus=controller-i2c0,address=13,"
            "initial-state=%s" % (out / "auxiliary.bin"),
        ):
            cmd += ["-device", device]
        for i in range(4):
            cmd += ["-device", "nikon-z72-controller-register-bank,"
                    "bus=master-i2c%d,address=104" % i]
        for cpu in range(4):
            cmd += ["-device", "loader,addr=0x10020000,cpu-num=%d" % cpu]
        if card:
            cmd += ["-global", "nikon-z72-boot-config.sd-host=on",
                    "-drive", "if=none,id=z62-card,file=%s,format=raw,snapshot=on" % card,
                    "-device", "sd-card,drive=z62-card,bus=/z72-sd-slave/sd-bus"]
        cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % ports["qtest"],
                "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % ports["qmp"],
                "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % ports["hmp"]]
        if cfg.get("trace"):
            cmd += ["-d", cfg["trace"], "-D", str(out / "trace.log")]
        elif cfg.get("logport"):
            # -D opens the log file; guest_errors keeps the mask quiet while
            # the log port's qemu_log() lines still land in it.
            cmd += ["-d", "guest_errors", "-D", str(out / "trace.log")]
        return cmd, ports, profile

    def start(self, cfg):
        with self.lock:
            if self.running():
                return dict(ok=False, error="already running")
            cmd, ports, profile = self.build_args(cfg)
            (RUN / "last-command.json").write_text(json.dumps(cmd, indent=1),
                                                   encoding="utf-8")
            self.log_stream = self.log_path.open("wb")
            env = qemu_env()
            if cfg.get("clear_status"):
                # The machine clears parameter 0x1921's status list at guest
                # 1.3 s; without it the menu gate stays shut and the Error box
                # covers the shooting screen.
                env["Z62_CLEAR_STATUS"] = "1"
            self.proc = subprocess.Popen(cmd, stdout=self.log_stream,
                                         stderr=subprocess.STDOUT, env=env,
                                         cwd=str(ROOT))
            self.started = time.time()
            self.profile = profile
            stale = WEBUI / "screen.png"
            if stale.exists():
                stale.unlink()
            # What this instance is actually running. Changing a selection while
            # a machine is up does not affect it, so the UI has to say so.
            self.effective = dict(profile=profile, master=cfg.get("master"),
                                  slave=cfg.get("slave"),
                                  controller=cfg.get("controller"),
                                  card=cfg.get("card"), aux=cfg.get("aux"),
                                  main_screen=bool(cfg.get("main_screen")))
            self.error = None
            try:
                if ports["qtest"]:
                    self.qtest = Channel(ports["qtest"])
                    self.qmp = Channel(ports["qmp"], greeting=True)
                    self.hmp = Hmp(ports["hmp"])
                    self._init_board()
            except Exception as error:
                self.error = "board init failed: %s" % error
                self.stop()
                return dict(ok=False, error=self.error)
        return dict(ok=True, command=cmd)

    def _init_board(self):
        listing = self.hmp.cmd("qom-list /machine/unattached")
        names = re.findall(r"(device\[\d+\]) \(child<nikon-z72-controller-gpio>\)", listing)
        self.gpios = sorted(names, key=lambda n: int(re.search(r"\d+", n).group()))
        if len(self.gpios) != 8:
            raise RuntimeError("expected 8 GPIO banks, got %d" % len(self.gpios))
        for bank, pin, level in POWER_INPUTS:
            self.qtest.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                           % (self.gpios[bank], pin, level))
        configs = re.findall(r"(device\[\d+\]) \(child<nikon-z72-boot-config>\)", listing)
        configs.sort(key=lambda n: int(re.search(r"\d+", n).group()))
        if configs:
            for level in (0, 1):
                self.qtest.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                               % (configs[0], level))

    def key(self, name, hold=0.25):
        pin = KEYS.get(name, name)
        if not self.running():
            return dict(ok=False, error="not running")
        wire = self.qtest
        wire.cmd("set_irq_in /machine/peripheral/keys pins %d 0" % pin)

        def release():
            if wire is self.qtest and self.running():
                wire.cmd("set_irq_in /machine/peripheral/keys pins %d 1" % pin)

        threading.Timer(hold, release).start()
        return dict(ok=True, key=name, pin=pin, hold=hold)

    def shot(self):
        """Capture the current frame; safe to call from several threads.

        The browser's 1 Hz poll drives this through GET /screen.png, so two
        requests can overlap; the lock also keeps the temporary PPM single-use.
        """
        if not self.running() or self.profile == "z6ii":
            return None
        with self._shot_lock:
            path = WEBUI / "screen.png"
            ppm = WEBUI / "screen.ppm"
            if ppm.exists():
                ppm.unlink()
            self.qmp.send({"execute": "screendump",
                           "arguments": {"filename": str(ppm)}})
            if not ppm.exists():
                return None
            data = ppm.read_bytes()
            parts = data.split(b"\n", 3)
            if len(parts) != 4:
                return None
            dimensions = parts[1].split()
            size, height = int(dimensions[0]), int(dimensions[1])
            body = parts[3]
            raw = b"".join(b"\x00" + body[y * size * 3:(y + 1) * size * 3]
                           for y in range(height))

            def chunk(kind, payload):
                return (struct.pack(">I", len(payload)) + kind + payload +
                        struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

            path.write_bytes(b"\x89PNG\r\n\x1a\n" +
                             chunk(b"IHDR", struct.pack(">IIBBBBB", size, height,
                                                        8, 2, 0, 0, 0)) +
                             chunk(b"IDAT", zlib.compress(raw, 3)) +
                             chunk(b"IEND", b""))
            return path

    def gpu_counters(self):
        if not self.running() or self.profile == "z6ii":
            return {}
        out = {}
        for name in ("successful-uploads", "rendered-commands",
                     "unsupported-commands", "failed-requests"):
            text = self.hmp.cmd("qom-get /machine/master-graphics " + name).strip()
            digits = re.findall(r"-?\d+", text.splitlines()[-1] if text else "")
            out[name] = int(digits[-1]) if digits else None
        return out

    def cores(self):
        """Per-core PC/LR, so a hung boot can be pinned to an address."""
        if not self.running() or not self.hmp:
            return []
        out = []
        for cpu in range(4 if self.profile != "z6ii" else 2):
            self.hmp.cmd("cpu %d" % cpu)
            text = self.hmp.cmd("info registers")
            found = dict(re.findall(r"(R\d{2}|PC|LR|SP)=([0-9a-f]{1,8})", text))
            out.append(dict(cpu=cpu, pc=found.get("R15") or found.get("PC"),
                            lr=found.get("R14") or found.get("LR")))
        self.hmp.cmd("cpu 0")
        return out

    def guest_seconds(self):
        """Elapsed guest time from the 48 MHz counter.

        Wall-clock seconds are meaningless under -icount shift=0: the guest only
        advances as fast as the host executes it, so two 60-second runs are not
        comparable. This is the number that is.
        """
        if not self.running() or self.profile == "z6ii":
            return None
        text = self.hmp.cmd("xp/2xw 0x04266008")
        # The telnet monitor echoes the typed command back, so only trust lines
        # that look like an xp result: "004266008: 0x... 0x...".
        rows = re.findall(r"^\s*[0-9a-f]{6,10}:\s+(.*)$", text, re.M)
        if not rows:
            return None
        words = re.findall(r"0x([0-9a-f]{1,8})\b", rows[-1])
        if len(words) < 2:
            return None
        ticks = (int(words[1], 16) << 32) | int(words[0], 16)
        return round(ticks / 48000000.0, 2)

    def stop(self):
        with self.lock:
            for channel in (self.qtest, self.qmp, self.hmp):
                if channel:
                    channel.close()
            self.qtest = self.qmp = self.hmp = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
            self.effective = None
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
        return dict(ok=True)

    def tail(self, limit=120):
        chunks = []
        for path in (Path(self.log_path) if self.log_path else None,
                     Path(self.log_path).with_name("trace.log")
                     if self.log_path else None):
            if path and path.exists():
                chunks.append("-- %s --" % path.name)
                chunks.append(path.read_text(errors="replace"))
        return ("\n".join(chunks) or "(no output)")[-limit * 200:]


MACHINE = Machine()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, indent=1, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, kind, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._bytes(HTML.replace("@@BUILD@@", BUILD).encode(),
                             "text/html; charset=utf-8")
        if path == "/screen.png":
            # Capture as part of the fetch: the browser poll is the refresh
            # clock, so replaying a file would show a stale frame from an
            # earlier run next to a fresh guest clock.
            if MACHINE.running():
                MACHINE.shot()
            png = WEBUI / "screen.png"
            if png.exists():
                return self._bytes(png.read_bytes(), "image/png")
            return self._bytes(b"", "image/png")
        if path == "/api/state":
            state = dict(build=BUILD, running=MACHINE.running(), profile=MACHINE.profile,
                         uptime=round(time.time() - MACHINE.started, 1)
                         if MACHINE.started else 0,
                         guest=MACHINE.guest_seconds(),
                         running_with=MACHINE.effective,
                         error=MACHINE.error, gpu=MACHINE.gpu_counters()
                         if MACHINE.running() else {},
                         inputs=inventory())
            return self._json(state)
        if path == "/api/log":
            return self._json(dict(log=MACHINE.tail()))
        if path == "/api/read":
            query = self.path.partition("?")[2]
            try:
                addr = int(query.split("addr=")[1].split("&")[0], 0)
                words = int(query.split("words=")[1].split("&")[0]) if "words=" in query else 8
            except (IndexError, ValueError):
                return self._json(dict(ok=False, error="addr= and words= required"))
            if not MACHINE.running() or not MACHINE.hmp:
                return self._json(dict(ok=False, error="not running"))
            words = max(1, min(words, 128))
            mode = "xp" if "va=1" not in query else "x"
            text = MACHINE.hmp.cmd("%s/%dxw 0x%08x" % (mode, words, addr))
            values = []
            for line in text.splitlines():
                if ":" not in line:
                    continue
                for token in line.split(":", 1)[1].split():
                    if token.startswith("0x"):
                        values.append(token)
            return self._json(dict(ok=True, addr=hex(addr), values=values))
        if path == "/api/hmp":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            cmd = (query.get("cmd") or [""])[0]
            if not MACHINE.running() or not MACHINE.hmp:
                return self._json(dict(ok=False, error="not running"))
            if not cmd.startswith(("find", "x ", "xp", "info ", "cpu ")):
                return self._json(dict(ok=False, error="command not allowed"))
            return self._json(dict(ok=True, text=MACHINE.hmp.cmd(cmd).strip()))
        if path == "/api/qtest":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            cmd = (query.get("cmd") or [""])[0]
            if not MACHINE.running() or not MACHINE.qtest:
                return self._json(dict(ok=False, error="not running"))
            if not cmd.startswith(("read ", "write ")):
                return self._json(dict(ok=False, error="command not allowed"))
            return self._json(dict(ok=True, reply=MACHINE.qtest.cmd(cmd)))
        if path == "/api/peek":
            query = self.path.partition("?")[2]
            addr = int(query.split("addr=")[1].split("&")[0], 0) if "addr=" in query else 0
            if not MACHINE.running() or not MACHINE.hmp:
                return self._json(dict(ok=False, error="not running"))
            text = MACHINE.hmp.cmd("xp/4xw 0x%08x" % addr)
            return self._json(dict(ok=True, addr=hex(addr), text=text.strip()))
        if path == "/api/regs":
            return self._json(dict(cores=MACHINE.cores()))
        if path == "/api/variants":
            return self._json(dict(variants=variants()))
        self.send_error(404)

    def do_POST(self):
        head, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        def arg(key):
            from urllib.parse import unquote
            return unquote(params.get(key, ""))

        if head == "/api/upload":
            INPUTS.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", arg("name") or "image.bin")
            slot = arg("slot") or "misc"
            dest = INPUTS / ("%s__%s" % (slot, name))
            dest.write_bytes(body)
            info = describe(dest)
            info.update(ok=True, verdict=classify(slot, info))
            if slot in ("master", "slave", "controller", "card"):
                pinned, failure = safe_pin(slot, dest)
                if failure:
                    info.update(ok=False, error=failure)
                else:
                    info.update(pinned)
            return self._json(info)
        if head == "/api/forget":
            payload = json.loads(body or b"{}")
            return self._json(dict(ok=True, **{"pinned": forget(payload.get("slot", "master"))}))
        if head == "/api/nor":
            payload = json.loads(body or b"{}")
            dest = WEBUI / "parameters"
            return self._json(extract_groups(payload.get("path", ""), dest))
        if head == "/api/scan":
            payload = json.loads(body or b"{}")
            return self._json(scan_nor(payload.get("path", "")))
        if head == "/api/variant":
            payload = json.loads(body or b"{}")
            path = payload.get("path", "")
            if payload.get("action") == "extract":
                info = extract_master(path)
                if info.get("ok"):
                    pinned, failure = safe_pin("master", info["path"])
                    if failure:
                        info.update(ok=False, error=failure)
                    else:
                        info.update(pinned)
                return self._json(info)
            target = Path(path) if Path(path).is_absolute() else ROOT / path
            info = describe(target)
            info.update(ok=info.get("present", False), slot="master",
                        verdict=classify("master", info))
            if info.get("ok"):
                total, runs = diff_runs(target)
                info.update(diff_bytes=total,
                            runs=[["0x%x" % a, n] for a, n in runs])
                pinned, failure = safe_pin("master", str(target))
                if failure:
                    info.update(ok=False, error=failure)
                else:
                    info.update(pinned)
            return self._json(info)
        if head == "/api/adopt":
            payload = json.loads(body or b"{}")
            info = adopt(payload.get("path", ""), payload.get("slot", ""))
            if info.get("ok") and payload.get("slot"):
                pinned, failure = safe_pin(payload["slot"], payload["path"])
                if failure:
                    info.update(ok=False, error=failure)
                else:
                    info.update(pinned)
            return self._json(info)
        if head == "/api/start":
            cfg = json.loads(body or b"{}")
            return self._json(MACHINE.start(cfg))
        if head == "/api/stop":
            return self._json(MACHINE.stop())
        if head == "/api/key":
            payload = json.loads(body or b"{}")
            return self._json(MACHINE.key(payload.get("key", "menu"),
                                          float(payload.get("hold", 0.25))))
        if head == "/api/shot":
            path = MACHINE.shot()
            return self._json(dict(ok=bool(path), path=str(path) if path else None))
        self.send_error(404)


def scan_nor(path):
    """Locate ENDM-terminated groups of the three known sizes at any base."""
    p = Path(path)
    if not p.is_file():
        return dict(error="not found")
    blob = p.read_bytes()
    found = {}
    for number, _, size in GROUPS:
        hits = []
        end = blob.find(FOOTER)
        while end != -1 and len(hits) < 6:
            start = end - size + 4
            if start >= 0 and blob[start:end].strip(b"\0"):
                hits.append(hex(start))
            end = blob.find(FOOTER, end + 1)
        found["group%d" % number] = hits
    return dict(size=len(blob), candidates=found)


def adopt(path, slot):
    """Report an existing local file so the UI can register it without copying."""
    p = Path(path)
    if not p.is_file():
        return dict(ok=False, error="not a file")
    info = describe(p)
    info.update(slot=slot, verdict=classify(slot, info), ok=True)
    return info


def inventory():
    out = {}
    pinned = load_selections()
    for slot in ("master", "slave", "controller", "card"):
        preferred = {
            "master": ROOT / "fw_verified" / "eg1850_mas_01700",
            "slave": ROOT / "fw_verified" / "eg1850_sla_01700",
            "controller": ROOT / "fw_verified" / "ex1850_012000.bi",
            "card": ROOT / ".local" / "sdcard.raw",
        }[slot]
        source = "默认"
        picked = None
        if pinned.get(slot) and Path(pinned[slot]).is_file():
            picked, source = Path(pinned[slot]), "已选定"
        elif preferred.is_file():
            picked = preferred
        else:
            for candidate in sorted(INPUTS.glob("%s__*" % slot)) if INPUTS.is_dir() else []:
                picked, source = candidate, "上传"
                break
        info = describe(picked) if picked else dict(present=False)
        info.update(verdict=classify(slot, info), source=source,
                    pinned=bool(pinned.get(slot)))
        out[slot] = info
    params = WEBUI / "parameters" / "group17.bin"
    real = ROOT / ".local" / "parameters_real" / "group17.bin"
    lab = ROOT / ".local" / "parameters_lab" / "group17.bin"
    chosen = params if params.is_file() else (real if real.is_file() else
                                             (lab if lab.is_file() else None))
    if chosen:
        base = chosen.parent
        out["parameters"] = dict(source=str(base),
                                 groups=[describe(base / ("group%d.bin" % n))
                                         for n, _, _ in GROUPS])
        out["parameters"]["aux"] = ",,".join(
            "%s/group%d.bin@%#x" % (str(base).replace("\\", "/"), n, a)
            for n, a, _ in GROUPS)
    else:
        out["parameters"] = dict(source=None, groups=[])
    return out


BUILD = time.strftime("%Y%m%d-%H%M", time.localtime(os.path.getmtime(__file__)))

HTML = r"""<!doctype html><meta charset=utf-8>
<title>Z6 II QEMU · 固件实验台</title>
<style>
:root{color-scheme:dark;--bg:#111417;--panel:#1a1f24;--line:#2b333a;--fg:#e6e9ec;--dim:#8b979f;--ok:#5ad17a;--warn:#e6b34a;--bad:#e06a6a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Segoe UI",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:baseline}
h1{font-size:16px;margin:0;font-weight:600}header .tag{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:minmax(420px,1fr) 420px;gap:18px;padding:18px 20px;align-items:start}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0 0 10px}
.row{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap}
input[type=text],select{flex:1;min-width:220px;background:#0d1013;border:1px solid var(--line);color:var(--fg);padding:7px 9px;border-radius:6px;font:12px ui-monospace,Consolas,monospace}
button{background:#252c33;border:1px solid var(--line);color:var(--fg);padding:7px 11px;border-radius:6px;cursor:pointer;font-size:13px}
select#slot{flex:0 0 132px;min-width:0}button:hover{background:#2f3841}button.primary{background:#1f5d3a;border-color:#2b7d4d}button.danger{background:#5d1f1f;border-color:#7d2b2b}
button.key{min-width:64px;font-size:15px;padding:11px}
.badge{font-size:11px;padding:2px 7px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:var(--ok);border-color:#245133}.badge.warn{color:var(--warn);border-color:#5a4520}.badge.bad{color:var(--bad);border-color:#5a2424}
#screen{width:100%;border:1px solid var(--line);border-radius:8px;background:#000;image-rendering:auto}
.kv{display:grid;grid-template-columns:150px 1fr;gap:3px 10px;font:12px ui-monospace,Consolas,monospace;color:var(--dim)}
.kv b{color:var(--fg);font-weight:500}
pre{background:#0d1013;border:1px solid var(--line);border-radius:6px;padding:9px;max-height:220px;overflow:auto;font:11px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap;margin:0}
label{font-size:12px;color:var(--dim);display:flex;gap:6px;align-items:center}
.note{font-size:12px;color:var(--dim);margin:6px 0 0}
</style>
<header><h1>Z6 II QEMU · 固件实验台</h1><span class="tag">导入新固件 / 换参数 / 看屏 / 按键 — 不校验镜像身份，仅提示</span></header>
<main>
<div>
  <section><h2>画面</h2>
    <img id="screen" alt="等待启动">
    <div class="row" style="margin-top:10px">
      <button id="shot">立即抓屏</button>
      <label><input type="checkbox" id="live" checked>自动刷新（1/s）</label>
      <span id="status" class="badge">未运行</span><span id="warn" class="badge bad" style="display:none"></span>
    </div>
    <div class="kv" id="gpu"></div>
  </section>
  <section><h2>操作</h2>
    <div class="row">
      <button class="key" data-k="menu">MENU</button>
      <button class="key" data-k="ok">OK</button>
      <button class="key" data-k="up">↑</button>
      <button class="key" data-k="down">↓</button>
      <button class="key" data-k="left">←</button>
      <button class="key" data-k="right">→</button>
      <label>按住(墙钟秒，≥2 才稳)<input type="text" id="hold" value="2" style="width:62px;flex:0"></label>
    </div>
    <div class="row"><label><input type="checkbox" id="mainscr">主屏实验开关（补齐实验室完成事件）</label></div>
    <div class="row"><label><input type="checkbox" id="logport" checked>日志端口 -D（配合 tracequeue 变体看 z62.qlog）</label></div>
    <div class="row"><label><input type="checkbox" id="clearstatus" checked>清状态列表 0x1921（解锁菜单；否则 HUD 上盖 Error 框）</label></div>
    <div class="row"><button class="primary" id="start">启动模拟器</button><button class="danger" id="stop">停止</button></div>
    <p class="note">按键经控制器 expander 注入：MENU=9 OK=3 ↑5 ↓4 ←7 →6。客体约 0.017× 实时，按住时长按墙钟算——0.25 秒只等于客体 4 毫秒，固件常常采样不到，建议 ≥2 秒。</p>
  </section>
  <section><h2>日志</h2><pre id="log">（尚未启动）</pre>
    <div class="row"><button id="reload">刷新日志</button></div></section>
</div>
<div>
  <section><h2>输入件</h2>
    <div id="inputs"></div>
    <div class="row" style="margin-top:10px">
      <select id="slot"><option value="master">master</option><option value="slave">slave</option><option value="controller">controller</option><option value="card">SD 卡</option></select>
      <input type="file" id="file"><button id="upload">上传</button>
    </div>
    <p class="note">上传保存到 <code>.local/webui/inputs/</code>；也可直接填本机绝对路径后“采用”。</p>
    <div class="row"><input type="text" id="adoptPath" placeholder="本机绝对路径，例如 E:\fw\eg1850_mas_01800.BIN"><button id="adopt">采用路径</button></div>
  </section>
  <section><h2>NOR 参数组</h2>
    <div class="row"><input type="text" id="norPath" placeholder="NOR_64MiB.bin 绝对路径" value=""><button id="norCheck">校验并导出</button><button id="norScan">反查位置</button></div>
    <pre id="nor">（未校验）</pre>
    <p class="note">要求 67,108,864 字节，且第 17/18/32 组各自以 ENDM 结尾。通过后写入 <code>.local/webui/parameters/</code> 并自动用于启动。</p>
  </section>
  <section><h2>补丁运行</h2>
    <div class="row"><select id="patchSel" style="flex:1;min-width:0"></select>
      <button class="primary" id="patchRun">运行所选补丁</button></div>
    <div class="kv" id="patchInfo"><span>状态</span><b>选一个补丁，一键换成 master 并启动</b></div>
    <p class="note">与“已完成的固件改动”同一批产物；这里只挑 master 模块，容器请先在下面“解出 master”。运行前会自动套用当前参数组与“清状态列表”开关。</p>
  </section>
  <section><h2>已完成的固件改动</h2>
    <div class="row"><button id="vReload">重新扫描 dist/</button><span id="vInfo" class="badge">—</span></div>
    <div id="variants"></div>
    <p class="note">master 模块（38,750,138 字节）可直接加载；容器（49,002,888）先“解出 master”。差异按原厂 master 逐字节计算，偏移是加载后的虚拟地址。</p>
  </section>
  <section><h2>机器</h2>
    <div class="row"><label>板型
      <select id="profile"><option value="z62-dual">nikon-z62-dual-lab（双引擎+M4，有显示）</option><option value="z6ii">nikon-z6ii（自研，仅 -kernel 无显示）</option></select></label></div>
    <div class="kv" id="ident"></div>
  </section>
</div>
</main>
<script>
const $=s=>document.querySelector(s);
const PAGE_BUILD="@@BUILD@@";
let INV={};
async function j(url,opt){const r=await fetch(url,opt);return r.json();}
function badge(v){const k=v.includes('stock C1.70')?'ok':(v.includes('non-stock')?'bad':'warn');return '<span class="badge '+k+'">'+v+'</span>';}
function renderInputs(){
  const rows=[['master','主引擎 mas'],['slave','从引擎 sla'],['controller','控制器 ex'],['card','SD 卡']];
  $('#inputs').innerHTML=rows.map(([k,label])=>{
    const i=INV[k]||{};const short=(i.path||'—').split(/[\\/]/).pop();
    const src=i.pinned?'<span class="badge warn" title="已固定，重启页面也不会退回原厂">'+(i.source||'已选定')+'</span>'
                      :'<span class="badge">'+(i.source||'默认')+'</span>';
    const reset=i.pinned?'<button data-forget="'+k+'" title="退回原厂镜像">恢复原厂</button>':'';
    return '<div class="row"><span style="width:88px;color:var(--dim)">'+label+'</span>'+
      '<input type="text" data-slot="'+k+'" value="'+(i.path||'')+'" title="'+(i.path||'')+'" placeholder="'+short+'">'+
      src+badge(i.verdict||'missing')+'<span class="badge">'+(i.size?Math.round(i.size/1024)+'K':'—')+'</span>'+reset+'</div>';
  }).join('')+
  '<div class="row"><span style="width:88px;color:var(--dim)">参数组</span><span class="badge '+(INV.parameters&&INV.parameters.source?'ok':'warn')+'">'+
  ((INV.parameters&&INV.parameters.source)||'缺省')+'</span></div>';
  document.querySelectorAll('input[data-slot]').forEach(el=>el.onchange=()=>{INV[el.dataset.slot]={path:el.value,verdict:'non-stock',size:0};renderInputs();});
  document.querySelectorAll('[data-forget]').forEach(b=>b.onclick=async()=>{
    await j('/api/forget',{method:'POST',body:JSON.stringify({slot:b.dataset.forget})});await reloadInv();});
}
async function refresh(){
  const st=await j('/api/state');INV=st.inputs||{};renderInputs();
  $('#status').textContent=st.running?('运行中 · 墙钟 '+st.uptime+'s · 客体 '+(st.guest===null?'—':st.guest)+'s'):'未运行';
  $('#status').className='badge '+(st.running?'ok':'');
  const g=st.gpu||{};$('#gpu').innerHTML=Object.keys(g).length?Object.entries(g).map(([k,v])=>'<span>'+k+'</span><b>'+v+'</b>').join(''):'';
  if($('#live').checked&&st.running){$('#screen').src='/screen.png?t='+Date.now();}
  const want=(INV.master&&INV.master.path)||'';
  const got=(st.running_with&&st.running_with.master)||'';
  const warn=$('#warn');
  if(st.running&&want&&got&&want!==got){warn.style.display='';warn.textContent='改动未生效：正在跑的是 '+got.split(/[\/]/).pop()+'，点停止再启动';}
  else if(st.build&&st.build!==PAGE_BUILD){warn.style.display='';warn.textContent='界面已更新，请 Ctrl+F5 刷新';}
  else warn.style.display='none';
  if(st.error)$('#log').textContent='ERROR: '+st.error;
}
function pickPaths(){const p={};document.querySelectorAll('input[data-slot]').forEach(el=>{if(el.value.trim())p[el.dataset.slot]=el.value.trim();});return p;}
$('#start').onclick=async()=>{const cfg=pickPaths();cfg.profile=$('#profile').value;cfg.main_screen=$('#mainscr').checked;cfg.logport=$('#logport').checked;cfg.clear_status=$('#clearstatus').checked;
  if(INV.parameters&&INV.parameters.aux)cfg.aux=INV.parameters.aux;
  const r=await j('/api/start',{method:'POST',body:JSON.stringify(cfg)});
  $('#log').textContent=r.ok?'已启动':JSON.stringify(r);refresh();};
$('#stop').onclick=async()=>{await j('/api/stop',{method:'POST'});refresh();};
$('#shot').onclick=async()=>{await j('/api/shot',{method:'POST'});$('#screen').src='/screen.png?t='+Date.now();};
document.querySelectorAll('.key').forEach(b=>b.onclick=()=>j('/api/key',{method:'POST',body:JSON.stringify({key:b.dataset.k,hold:parseFloat($('#hold').value)||0.25})}));
$('#reload').onclick=async()=>{const r=await j('/api/log');$('#log').textContent=r.log.slice(-6000);};
$('#upload').onclick=async()=>{const f=$('#file').files[0];if(!f){alert('先选文件');return;}
  await fetch('/api/upload?slot='+$('#slot').value+'&name='+encodeURIComponent(f.name),{method:'POST',body:f});
  await reloadInv();};
$('#adopt').onclick=async()=>{const p=$('#adoptPath').value.trim();if(!p)return;
  const r=await j('/api/adopt',{method:'POST',body:JSON.stringify({path:p,slot:$('#slot').value})});
  if(r.ok){INV[$('#slot').value]=r;renderInputs();}else alert(r.error);};
$('#norCheck').onclick=async()=>{const r=await j('/api/nor',{method:'POST',body:JSON.stringify({path:$('#norPath').value.trim()})});
  $('#nor').textContent=JSON.stringify(r,null,1);await reloadInv();};
$('#norScan').onclick=async()=>{const r=await j('/api/scan',{method:'POST',body:JSON.stringify({path:$('#norPath').value.trim()})});
  $('#nor').textContent=JSON.stringify(r,null,1);};
async function reloadInv(){const st=await j('/api/state');INV=st.inputs||{};renderInputs();
  $('#norPath').value=$('#norPath').value||'';}
function renderVariants(list){
  $('#variants').innerHTML=list.map(v=>{
    const runs=(v.runs||[]).slice(0,3).map(r=>r[0]+'+'+r[1]).join(' ');
    const state=v.kind==='master'
      ? (v.is_stock?'<span class="badge ok">与原厂一致</span>':'<span class="badge warn">改 '+v.diff_bytes+' 字节</span>')
      : '<span class="badge">容器</span>';
    const act=v.kind==='master'
      ? '<button data-adopt="'+v.path+'">选用</button>'
      : '<button data-extract="'+v.path+'">解出 master</button>';
    return '<div class="row"><span style="width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+v.path+'">'+v.path.replace(/^dist\//,'')+'</span>'+
      state+'<span class="badge">'+v.label+' '+(v.sha256||'').slice(0,8)+'</span>'+
      '<span style="flex:1;font:11px ui-monospace,Consolas;color:var(--dim)">'+runs+'</span>'+act+'</div>';
  }).join('');
  document.querySelectorAll('[data-adopt]').forEach(b=>b.onclick=async()=>{
    const r=await j('/api/variant',{method:'POST',body:JSON.stringify({path:b.dataset.adopt})});
    if(r.ok){INV.master=r;renderInputs();$('#log').textContent='已选用 '+b.dataset.adopt;}
  });
  document.querySelectorAll('[data-extract]').forEach(b=>b.onclick=async()=>{
    const r=await j('/api/variant',{method:'POST',body:JSON.stringify({path:b.dataset.extract,action:'extract'})});
    if(r.ok){INV.master=r;renderInputs();
      $('#log').textContent='已解出 '+r.path+'（相对原厂改 '+(r.diff_bytes||0)+' 字节）';}
    else $('#log').textContent='解出失败：'+r.error;
  });
}
async function loadPatches(){
  const r=await j('/api/variants'); const list=(r.variants||[]).filter(v=>v.kind==='master');
  $('#patchSel').innerHTML=list.map(v=>'<option value="'+v.path+'">'+v.path.replace(/^dist\//,'')+'  ('+(v.diff_bytes===0?'原厂':'改 '+v.diff_bytes+' 字节')+')</option>').join('');
}
$('#patchRun').onclick=async()=>{
  const path=$('#patchSel').value; if(!path){alert('没有可用补丁');return;}
  const info=$('#patchInfo');
  info.innerHTML='<span>状态</span><b>正在切换并启动 '+path+'</b>';
  const r=await j('/api/variant',{method:'POST',body:JSON.stringify({path})});
  if(!r.ok){info.innerHTML='<span>状态</span><b>选用失败：'+(r.error||'')+'</b>';return;}
  await startMachine();
  info.innerHTML='<span>状态</span><b>已启动 '+path+'（改动 '+(r.diff_bytes||0)+' 字节）</b>';
};
async function startMachine(){
  const cfg=pickPaths();cfg.profile=$('#profile').value;
  cfg.main_screen=$('#mainscr').checked;cfg.logport=$('#logport').checked;
  cfg.clear_status=$('#clearstatus').checked;
  if(INV.parameters&&INV.parameters.aux)cfg.aux=INV.parameters.aux;
  const r=await j('/api/start',{method:'POST',body:JSON.stringify(cfg)});
  $('#log').textContent=r.ok?'已启动':JSON.stringify(r);refresh();
}
async function loadVariants(){const r=await j('/api/variants');renderVariants(r.variants||[]);
  $('#vInfo').textContent=(r.variants||[]).length+' 个产物';}
$('#vReload').onclick=loadVariants;
reloadInv();loadVariants();loadPatches();setInterval(refresh,1000);
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args()
    for folder in (WEBUI, INPUTS, RUN):
        folder.mkdir(parents=True, exist_ok=True)
    if not QEMU.is_file():
        raise SystemExit("QEMU not found: %s" % QEMU)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Z6 II 固件实验台 → http://127.0.0.1:%d" % args.port)
    print("qemu: %s" % QEMU)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MACHINE.stop()


if __name__ == "__main__":
    main()
