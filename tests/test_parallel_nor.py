# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the optional AMD CFI NOR commands and actual backing persistence."""
import os
import argparse
from pathlib import Path
import socket
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--qemu', required=True)
args = parser.parse_args()
BASE = 0x10000000
TARGET = BASE + 0x2a20000

with tempfile.TemporaryDirectory(prefix='z62-nor-') as folder:
    image = Path(folder) / 'private.raw'
    with image.open('wb') as output:
        for _ in range(64): output.write(b'\xff' * 1024**2)
    for boot in range(2):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([args.qemu, '-M', 'nikon-z72-lab',
            '-accel', 'qtest', '-display', 'none', '-serial', 'none',
            '-monitor', 'none', '-nic', 'none',
            '-global', 'nikon-z72-boot-config.master-parallel-nor=on',
            '-drive', f'if=pflash,index=0,file={image},format=raw',
            '-qtest', f'tcp:127.0.0.1:{port},server=on,wait=off',
            '-qtest-log', os.devnull], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wire = None
        try:
            deadline = time.monotonic() + 10
            while wire is None:
                try:
                    wire = socket.create_connection(('127.0.0.1', port), timeout=2)
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError(process.stderr.read().decode())
                    if time.monotonic() >= deadline: raise
                    time.sleep(.05)
            stream = wire.makefile('rwb')
            def q(text):
                stream.write(text.encode() + b'\n')
                stream.flush()
                reply = stream.readline().decode().split()
                assert reply[0] == 'OK', reply
                return int(reply[1], 0) if len(reply) > 1 else None
            def write(address, value): q(f'writew {address:#x} {value:#x}')
            def read(address): return q(f'readw {address:#x}')
            def unlock(command):
                write(BASE+0xaaa, 0xaa)
                write(BASE+0x554, 0x55)
                write(BASE+0xaaa, command)
            def program(address, value):
                unlock(0xa0)
                write(address, value)
            if boot == 0:
                write(BASE+0xaa, 0x98)
                assert [read(BASE+i*2) for i in (0x10, 0x11, 0x12)] == [0x51, 0x52, 0x59]
                assert read(BASE+0x27*2) == 26
                assert read(BASE+0x2a*2) == 0, 'Do not advertise unsupported buffered programming'
                assert read(BASE+0x2d*2) | read(BASE+0x2e*2) << 8 == 511
                assert read(BASE+0x2f*2) | read(BASE+0x30*2) << 8 == 512
                write(BASE, 0xf0)
                program(TARGET, 0x1234)
                program(TARGET, 0xffff)
                assert read(TARGET) == 0x1234, 'Programming set cleared NOR bits'
                program(TARGET+0x20000, 0xbeef)
                unlock(0x80)
                write(BASE+0xaaa, 0xaa)
                write(BASE+0x554, 0x55)
                write(TARGET, 0x30)
                q('clock_step 2000000000')
                assert read(TARGET) == 0xffff
                assert read(TARGET+0x20000) == 0xbeef, 'Erase damaged neighboring sector'
                program(TARGET, 0xb26a)
                assert read(TARGET) == 0xb26a
            else:
                assert read(TARGET) == 0xb26a, 'New process lost programmed contents'
                assert read(TARGET+0x20000) == 0xbeef
        finally:
            if wire: wire.close()
            process.terminate()
            process.wait(timeout=5)
    with image.open('rb') as source:
        source.seek(TARGET-BASE)
        assert source.read(2) == b'\x6a\xb2'
    print('PASS AMD CFI query, word program, sector erase, bit constraints and new-process persistence')
