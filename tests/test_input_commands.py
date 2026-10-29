#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check armed input stop/drain responses with a synthetic ARM guest."""
import argparse
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import connect, free_port, monitor


def check(qemu, enabled):
    with tempfile.TemporaryDirectory(prefix='input-commands-') as directory:
        folder = Path(directory)
        port = free_port()
        with (folder / 'qemu.log').open('wb') as log:
            process = subprocess.Popen([str(qemu), '-M', 'nikon-z62-dual-lab',
                '-smp', '4', '-display', 'none', '-serial', 'none', '-monitor',
                'none', '-nic', 'none', '-S', '-gdb', f'tcp:127.0.0.1:{port}',
                '-icount', 'shift=0,sleep=off', '-global',
                'nikon-z72-boot-config.input-command-completion=' +
                ('on' if enabled else 'off')],
                stdout=subprocess.DEVNULL, stderr=log)
            remote = None
            try:
                remote = connect(process, port, folder / 'qemu.log')
                base = 0x10020000
                code = struct.pack('<3I', 0xe5810000, 0xe5912000, 0xeafffffe)
                for thread in (1, 3):
                    remote.select_thread(thread)
                    remote.write(base, code)

                def access(thread, address, value=None):
                    remote.select_thread(thread)
                    remote.set(pc=base + (4 if value is None else 0),
                               cpsr=0xd3, r0=value or 0, r1=address)
                    remote.command(f'vCont;s:{thread:x}')
                    return remote.get(remote.regs(), 'r2')

                def advance():
                    for thread in range(1, 5):
                        remote.select_thread(thread)
                        remote.set(pc=base + 8, cpsr=0xd3)
                    remote.sock.settimeout(.15)
                    try:
                        remote.command('vCont;c')
                        raise AssertionError('Unexpected synthetic guest stop')
                    except TimeoutError:
                        remote.sock.settimeout(15)
                        remote.interrupt()

                def pending(thread, irq):
                    return bool(access(thread, 0x04001200 + irq // 32 * 4) &
                                (1 << (irq % 32)))

                cases = ((0x20, 8, 0, 0x10000000, 94),
                         (0x10, 4, 1, 1, 94),
                         (0x600, 3, 1, 0x110, 94),
                         (0x800, 0x10, 1, 0x1000, 94),
                         (2, 0x60, 7, 0x30, 98))
                for thread, peer in ((1, 3), (3, 1)):
                    for command, control, bank, bits, irq in cases:
                        mask = 0x20068200 + bank * 0x20
                        status = mask + 0x10

                        def clear():
                            access(thread, status, 0xffffffff)
                            access(thread, 0x04001280 + irq // 32 * 4,
                                   1 << (irq % 32))

                        def configure(enable, armed):
                            access(thread, 0x2006800c, command)
                            access(thread, 0x20068018, enable)
                            access(thread, mask, armed)

                        configure(command, bits)
                        advance()
                        assert access(thread, status) == 0, 'Configuration generated ack'
                        access(thread, 0x20068060, control ^ 0x80000000)
                        advance()
                        assert access(thread, status) == 0, 'Wrong control generated ack'
                        configure(0, bits)
                        access(thread, 0x20068060, control)
                        advance()
                        assert access(thread, status) == 0, 'Unenabled consumer generated ack'
                        configure(command, 0)
                        access(thread, 0x20068060, control)
                        advance()
                        assert access(thread, status) == 0, 'Unarmed consumer generated ack'

                        configure(command, bits)
                        access(thread, 0x20068060, control)
                        assert access(thread, status) == 0, 'Response was not delayed'
                        # Masking after a valid submission retains the event,
                        # but must not assert an interrupt until unmasked.
                        access(thread, mask, 0)
                        advance()
                        assert access(thread, status) == (bits if enabled else 0)
                        assert not pending(thread, irq)
                        access(thread, mask, bits)
                        assert pending(thread, irq) == enabled
                        assert access(peer, status) == 0 and not pending(peer, irq)
                        clear()
                        advance()
                        assert access(thread, status) == 0, 'Ack replayed after W1C'

                        configure(command, bits)
                        access(thread, 0x20068060, control)
                        access(thread, 0x2006800c, 0)
                        advance()
                        assert access(thread, status) == 0, 'Replaced command completed'
                        configure(command, bits)
                        access(thread, 0x20068060, control)
                        access(thread, 0x20068018, 0)
                        advance()
                        assert access(thread, status) == 0, 'Cancelled consumer completed'
                        access(thread, mask, 0)
                    assert access(thread, 0x2006807c) == 0
                    assert access(thread, 0x20065544) == 0

                access(1, 0x2006800c, 0x20)
                access(1, 0x20068018, 0x20)
                access(1, 0x20068200, 0x10000000)
                access(1, 0x20068060, 8)
                monitor(remote, 'system_reset')
                advance()
                assert access(1, 0x20068210) == 0 and not pending(1, 94)
                print('PASS input command responses ' + ('ON' if enabled else 'OFF') +
                      ': five exact command/control contracts, delayed armed completion, '
                      'mask retention, W1C, no replay, cancellation, reset and engine isolation')
            finally:
                if remote:
                    remote.sock.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--enabled', action='store_true')
    args = parser.parse_args()
    check(args.qemu, args.enabled)
