"""Unit-test the combo45 PTP read-permission hole inside the real firmware.

combo45 replaces the vendor-command admission branch with a jump to a code
cave at 0x10930E00:

    cbnz r0, allow              ; session already open
    movw r1,#0xf032 ; cmp sb,r1 ; beq allow     <- the only added case
    movw r1,#0xf001 ; cmp sb,r1 ; beq allow     <- original special case
    movw r4,#0xa004 ; b.w deny                   ; everything else

The cave is a pure function of (r0, sb): no memory, no callbacks. So instead of
needing a USB stack we can drive it directly - pause the guest after the
foreground is up, aim PC at the cave, set r0/sb, and see which exit it takes.

Exit allow  = 0x10930E1A   (b.w to the dispatcher's allow path)
Exit deny   = 0x104C96C0   (the 0xA004 error path)

That gives an end-to-end check of the patch on the real image: F032 must be
allowed with r0=0, F033 must not.

  python ptp_gate_probe.py --at 32
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

QEMU = r"D:\z6ii_fw_lab\tools\src\qemu\build\qemu-system-arm.exe"
MSYS = r"D:\z6ii_fw_lab\tools\msys64"
ROOT = r"D:\z6ii_fw_lab"

CAVE_ENTRY = 0x10930E00
EXIT_ALLOW = 0x10930E1A
EXIT_DENY = 0x104C96C0

GPIO_INPUTS = ((2, 5, 0), (3, 13, 1), (4, 9, 1), (6, 7, 1),
               (1, 11, 1), (6, 14, 1), (6, 15, 1))


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
            s.settimeout(20)
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
    """GDB remote serial protocol, no-ack mode, with proper + / - handling.

    The earlier cap_read_probe client ignored the ack characters, so packets
    silently desynchronised and every watchpoint looked like a miss. This one
    strips them before looking for '$'.
    """

    def __init__(self, port):
        self.s = tcp(port)
        self.buf = b""

    def _strip(self):
        while self.buf[:1] in (b"+", b"-"):
            self.buf = self.buf[1:]
            self.last_ack = self.buf[:0]

    def read(self, timeout):
        self.s.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            self._strip()
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

    def cmd(self, text, timeout=4.0):
        p = text.encode()
        self.s.sendall(b"$" + p + b"#%02x" % (sum(p) & 0xFF))
        return self.read(timeout)

    def send(self, text):
        p = text.encode()
        self.s.sendall(b"$" + p + b"#%02x" % (sum(p) & 0xFF))

    def interrupt(self):
        self.s.sendall(b"\x03")


def parse_regs(g):
    """g packet: 16 or 17 little-endian 32-bit registers."""
    if not g:
        return None
    try:
        raw = bytes.fromhex(g)
    except ValueError:
        return None
    if len(raw) < 16 * 4:
        return None
    out = {}
    for i in range(16):
        out["R%d" % i] = int.from_bytes(raw[i * 4:i * 4 + 4], "little")
    if len(raw) >= 17 * 4:
        out["CPSR"] = int.from_bytes(raw[16 * 4:17 * 4], "little")
    return out


def write_regs(rsp, regs):
    """Write all registers with G (hex blob), not P.

    P is the single-register form (P n=r); using it with a blob silently gets
    ignored by the stub, which is why every probe step then ran from a PC we
    never set.
    """
    order = ["R%d" % i for i in range(16)]
    blob = b"".join(regs[r].to_bytes(4, "little") for r in order)
    if regs.get("CPSR") is not None:
        blob += regs["CPSR"].to_bytes(4, "little")
    return rsp.cmd("G" + blob.hex())


def run_case(rsp, opcode, enter_r0):
    """Aim PC at the cave with sb=opcode, r0=enter_r0, single-step to an exit.

    The cave is only eight instructions and branches with b.w rather than
    returning, so single-stepping is both simpler and more reliable than
    setting breakpoints on the two exit addresses (QEMU's Z0 with a Thumb
    address is easy to get wrong).
    """
    saved = parse_regs(rsp.cmd("g"))
    if not saved:
        return {"error": "no registers"}

    probe = dict(saved)
    probe["R15"] = CAVE_ENTRY | 1      # Thumb
    probe["R9"] = opcode               # sb is r9 in ARM, not r11
    probe["R0"] = enter_r0             # stand-in for the callback result
    # The cave is Thumb code, but the guest idles in ARM state (CPSR T bit
    # clear). Setting PC to a Thumb address without setting T lands on the
    # undefined-instruction vector at 0x10100004 and everything downstream
    # looks like a miss. Force T, and mask IRQ/FIQ so the single steps cannot
    # be hijacked mid-sequence.
    cpsr = saved.get("CPSR", 0x60000013)
    probe["CPSR"] = (cpsr | 0x20 | 0xC0)
    if write_regs(rsp, probe) is None:
        write_regs(rsp, saved)
        return {"error": "G write rejected"}

    path = []
    verdict = "unexpected"
    for _ in range(12):
        rsp.send("s")
        stop = rsp.read(2.5)
        if stop is None:
            break
        cur = parse_regs(rsp.cmd("g"))
        if not cur:
            break
        pc = cur["R15"]
        path.append(pc)
        if pc in (EXIT_ALLOW, EXIT_ALLOW | 1):
            verdict = "allow"
            break
        if pc in (EXIT_DENY, EXIT_DENY | 1):
            verdict = "deny"
            break

    write_regs(rsp, saved)             # restore before anything else
    return {"hit": path[-1] if path else None, "verdict": verdict,
            "path": path}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=32.0,
                    help="seconds to let the guest boot before probing")
    ap.add_argument("--image", default=r"work\combo45\mas_combo45.bin",
                    help="MAIN module to probe (default: the combo45 build)")
    ap.add_argument("--port-base", type=int, default=4620)
    args = ap.parse_args()

    cmd = [QEMU, "-M",
           ("nikon-z62-dual-lab,controller-firmware=fw_verified/ex1850_012000.bi,"
            "controller-link=on,controller-battery-peer=on,"
            "master-boot-image=%s,slave-boot-image=fw_verified/eg1850_sla_01700,"
            "master-boot-aux=.local/parameters_lab/group17.bin@0x132c0000,,"
            ".local/parameters_lab/group18.bin@0x132e0000,,"
            ".local/parameters_lab/group32.bin@0x13e80000,"
            "controller-flash-data=.local/z62-smoke/resident.bin" % args.image),
           "-smp", "4,maxcpus=5", "-display", "none", "-serial", "none",
           "-nic", "none", "-icount", "shift=0,sleep=off",
           "-global", "nikon-z72-boot-config.frame-sync=on",
           "-global", "nikon-z72-boot-config.frame-start=on",
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
           "-global", "nikon-z72-controller-adc.channel3=3001"]
    for c in range(4):
        cmd += ["-device", "loader,addr=0x10020000,cpu-num=%d" % c]

    hmp_port = args.port_base + 7
    gdb_port = args.port_base + 8
    qtest_port = args.port_base
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port,
            "-gdb", "tcp:127.0.0.1:%d,server=on,wait=off" % gdb_port]

    log = open(os.path.join(ROOT, r".local\z62-smoke\ptpgate.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env())
    t0 = time.time()
    try:
        hmp = Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        import re
        names = re.findall(r"(device\[\d+\]) \(child<nikon-z72-controller-gpio>\)",
                           listing)
        names = sorted(names, key=lambda n: int(re.search(r"\d+", n).group()))
        wire = Qtest(qtest_port)
        for bank, pin, level in GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (names[bank], pin, level))
        print("gpio asserted; booting %.0fs ..." % args.at)
        time.sleep(args.at)

        rsp = Rsp(gdb_port)
        rsp.read(1.0)
        rsp.send("QStartNoAckMode")
        rsp.read(1.0)
        rsp.interrupt()
        stop = rsp.read(4.0)
        regs = parse_regs(rsp.cmd("g"))
        if not regs:
            print("无法读取寄存器，停止")
            return 1
        print("已暂停: cpu0 PC=0x%08x" % regs["R15"])

        cases = [
            ("F032 读内存（应放行）", 0xF032, 0, "allow"),
            ("F033 写内存（应拒绝）", 0xF033, 0, "deny"),
            ("F001 开会话（应放行）", 0xF001, 0, "allow"),
            ("F002 任意其它（应拒绝）", 0xF002, 0, "deny"),
            ("F032 且会话已开（应放行）", 0xF032, 1, "allow"),
            ("F033 且会话已开（应放行）", 0xF033, 1, "allow"),
        ]
        print("\n=== 洞内分支测试 (cave 0x%08x) ===" % CAVE_ENTRY)
        all_ok = True
        for label, opcode, r0, expect in cases:
            res = run_case(rsp, opcode, r0)
            if "error" in res:
                print("  %-28s ERROR %s" % (label, res["error"]))
                all_ok = False
                continue
            ok = res["verdict"] == expect
            all_ok = all_ok and ok
            print("  %-28s opcode=0x%04x r0=%d -> %-8s (期望 %-5s) %s"
                  % (label, opcode, r0, res["verdict"], expect,
                     "PASS" if ok else "FAIL"))
            if not ok and res.get("path"):
                print("      单步路径: %s"
                      % " -> ".join("0x%08x" % p for p in res["path"][:8]))

        print("\n结果:", "PASS" if all_ok else "FAIL")
        return 0 if all_ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
