#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The optional sampling source must preserve guest configuration and its input."""
import os
import argparse
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.dual_boot_probe import dual_machine
from tools.z62_probe import monitor, free_port, qtest


def check(qemu, frame_start=False):
    image = struct.pack('<3I', 0xe5810000, 0xe5912000, 0xeafffffe)
    with tempfile.TemporaryDirectory(prefix='sensor-source-') as temporary:
        folder = Path(temporary)
        port = free_port()
        process = remote = wire = None
        try:
            process, remote = dual_machine(str(qemu), folder, image, image,
                folder / 'console.txt', icount=None, trace=None,
                extra_arguments=('-qtest', f'tcp:127.0.0.1:{port},server=on,wait=off',
                    '-qtest-log', os.devnull,
                    '-global', 'nikon-z72-boot-config.frame-sync=on',
                    '-global', 'nikon-z72-boot-config.sensor-sample=on',
                    '-global', 'nikon-z72-boot-config.frame-start=' + ('on' if frame_start else 'off'),
                    '-global', 'nikon-z72-boot-config.sample-lines=4660'))
            wire = socket.create_connection(('127.0.0.1', port), timeout=10)
            listing = monitor(remote, 'qom-list /machine/unattached')
            names = re.findall(r'(device\[\d+\]) \(child<nikon-z72-boot-config>\)', listing)
            names.sort(key=lambda value: int(re.search(r'\d+', value)[0]))
            assert len(names) == 2

            def access(address, value=None):
                remote.select_thread(1)
                remote.set(pc=0x10020004 if value is None else 0x10020000,
                           cpsr=0xd3, r0=value or 0, r1=address)
                remote.command('vCont;s:1')
                return remote.get(remote.regs(), 'r2')

            def advance():
                # Keep the test guest in its local branch loop. Other cores
                # have interrupts masked too; no original firmware is used.
                for thread in range(1, 5):
                    remote.select_thread(thread)
                    remote.set(pc=0x10020008, cpsr=0xd3)
                remote.sock.settimeout(.25)
                try:
                    remote.command('vCont;c')
                    raise AssertionError('Unexpected stop')
                except TimeoutError:
                    remote.sock.settimeout(15)
                    remote.interrupt()
                remote.select_thread(1)

            access(0x20061000, 0)
            qtest(wire, f'set_irq_in /machine/unattached/{names[0]} frame-sync-enable 0 1')
            advance()
            assert access(0x20061000) == 0, 'Source overwrote guest sampling configuration'
            assert access(0x20060044) == (0x90 if frame_start else 0x10)
            assert [access(0x20061300 + index * 4) for index in range(5)] == [4660] * 5
            # This read used to replace sample-lines with a register address.
            access(0x20068060)
            advance()
            assert [access(0x20061300 + index * 4) for index in range(5)] == [4660] * 5
            assert access(0x20061000) == 0
            remote.select_thread(3)
            assert int.from_bytes(remote.read(0x20061300, 4), 'little') == 0
            print('PASS sampling source: input-path reads preserve line values; '
                  'no guest configuration rewrite; engine isolation')
        finally:
            if wire:
                wire.close()
            if remote:
                remote.sock.close()
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--frame-start', action='store_true')
    args = parser.parse_args()
    check(args.qemu, args.frame_start)
