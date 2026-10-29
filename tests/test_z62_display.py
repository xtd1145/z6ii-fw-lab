#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise actual Z62 LCD banks, NV12, overlays, IRQs and reset in QEMU."""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import connect, monitor
from tools.smp_boot_probe import reach


def check(qemu, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = [str(qemu), '-M', 'nikon-z62-dual-lab', '-smp', '4',
               '-display', 'none', '-serial', 'none', '-monitor', 'none', '-nic', 'none',
               '-S', '-gdb', f'tcp:127.0.0.1:{port}', '-icount', 'shift=4,sleep=off']
    log = destination / 'qemu.log'
    checks = []
    remote = None
    with log.open('wb') as output:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=output)
        try:
            remote = connect(process, port, log)
            remote.select_thread(1)
            base = 0x10020000
            # STR r0,[r1]; LDR r2,[r1]; SUBS r0,#1; BNE -3; B .
            remote.write(base, struct.pack('<5I', 0xe5810000, 0xe5912000,
                                          0xe2500001, 0x1afffffd, 0xeafffffe))

            def access(address, value=None):
                remote.set(pc=base + (4 if value is None else 0), cpsr=0xd3,
                           r0=0 if value is None else value, r1=address)
                remote.command('s')
                return remote.get(remote.regs(), 'r2')

            def advance():
                remote.set(pc=base + 8, cpsr=0xd3, r0=3000000)
                reach(remote, base + 16, 'vCont;c:1')

            def frame(name):
                path = destination / (name + '.ppm')
                monitor(remote, 'screendump ' + str(path))
                data = path.read_bytes()
                header = data.split(b'\n', 3)
                assert header[:3] == [b'P6', b'8 4', b'255'], header[:3]
                pixels = header[3]
                assert len(pixels) == 8 * 4 * 3
                return lambda x, y: tuple(pixels[(y * 8 + x) * 3:(y * 8 + x + 1) * 3])

            lcd = 0x20144000
            y0, y1, uv0, uv1 = 0xc2001000, 0xc2001100, 0xc2001200, 0xc2001300
            remote.write(y0, bytes([16]) * 32)
            remote.write(y1, bytes([81]) * 32)
            remote.write(uv0, bytes([128]) * 16)
            remote.write(uv1, bytes([90, 240]) * 8)
            for offset, value in ((0x110, 1), (0x200, 0x300004), (0x204, 8 | (4 << 16)),
                (0x210, y0), (0x214, y1), (0x220, uv0), (0x224, uv1),
                (0x240, 8), (0x250, 8), (0x260, 1 << 8),
                (0x1100, 2), (0x1210, 1 << 16), (0x1200, 1)):
                access(lcd + offset, value)
            advance()
            pixel = frame('bank1-nv12')
            assert pixel(0, 0)[0] > 245 and max(pixel(0, 0)[1:]) < 5, pixel(0, 0)
            assert pixel(7, 3) == pixel(0, 0)
            checks.append('active Y/UV bank 1 and half-height NV12 chroma')
            assert access(lcd + 0x1214) & (1 << 16)
            assert access(0x0400120c) & (1 << 16), 'LCD mode 2 did not assert IRQ112'
            access(lcd + 0x1214, 1 << 16)
            assert not access(lcd + 0x1214) & (1 << 16)
            checks.append('VSync routes to IRQ112 and status is write-one-to-clear')

            color, alpha = 0xc2002000, 0xc2002100
            remote.write(color, bytes([235, 235, 128, 128]) * 2)
            remote.write(alpha, bytes([255, 0, 128, 255]))
            for offset, value in ((0x2110, 1), (0x2200, 0x30006), (0x2214, 2 << 8),
                (0x2400, 2 | (2 << 16)), (0x2448, color), (0x24c0, alpha),
                (0x2500, 4), (0x2540, 2), (0x2580, 2 | (1 << 16)), (0x25c0, 1)):
                access(lcd + offset, value)
            advance()
            pixel = frame('overlay-alpha')
            assert min(pixel(2, 1)) > 245, pixel(2, 1)
            assert pixel(3, 1)[0] > 245 and max(pixel(3, 1)[1:]) < 5, pixel(3, 1)
            assert 120 < pixel(2, 2)[1] < 140, pixel(2, 2)
            assert (access(lcd + 0x2214) >> 16) & 3 == 2
            checks.append('overlay bank 2, positioned YYUV422A and per-pixel alpha')

            green, opaque = 0xc2002200, 0xc2002300
            remote.write(green, bytes([145, 145, 54, 34]))
            remote.write(opaque, bytes([255, 255]))
            for offset, value in ((0x3110, 1), (0x3200, 0x30006),
                (0x3400, 2 | (1 << 16)), (0x3440, green), (0x34c0, opaque),
                (0x3500, 4), (0x3540, 2), (0x3580, 0), (0x35c0, 1)):
                access(lcd + offset, value)
            advance()
            pixel = frame('two-overlays')
            assert pixel(0, 0)[1] > 245 and pixel(0, 0)[0] < 5, pixel(0, 0)
            assert min(pixel(2, 1)) > 245
            checks.append('two independently configured overlay planes')
            access(lcd + 0x3440, 0x20144000)
            advance()
            pixel = frame('invalid-overlay')
            assert pixel(0, 0)[0] > 245 and pixel(0, 0)[1] < 5
            checks.append('MMIO overlay DMA source rejected')
            monitor(remote, 'system_reset')
            assert not access(lcd + 0x204) and not access(lcd + 0x1214)
            assert json.loads(monitor(remote, 'qom-get /machine/z62-lcd scanned-frames')) == 0
            checks.append('reset clears display configuration and counters')
        finally:
            if remote is not None:
                remote.sock.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    (destination / 'report.json').write_text(json.dumps(dict(passed=checks), indent=2))
    print('PASS Z62 display:', '; '.join(checks))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    check(args.qemu.resolve(), args.destination.resolve())
