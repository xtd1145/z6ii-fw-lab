# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the shared Z62 SD detect input with synthetic ARM loads/stores."""
import argparse
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze.decode_z72 import require
from tools.dual_boot_probe import dual_machine

GPIO = 0x04282014
DETECT = 1 << 14
BASE = 0x10020000


def monitor(remote, command):
    reply = remote.command('qRcmd,' + command.encode().hex())
    output = bytearray()
    while reply.startswith('O') and reply != 'OK':
        output.extend(bytes.fromhex(reply[1:]))
        reply = remote.receive()
    require(reply == 'OK', f'Monitor command failed: {command}: {reply}')
    require(not output.strip(), f'Monitor command failed: {output.decode()}')


def check(qemu, board, hosts, card_engine):
    label = f'{board}-' + (f'card-{card_engine}' if card_engine else
                          'empty-hosts' if hosts else 'no-host')
    with tempfile.TemporaryDirectory(prefix='z62-card-detect-') as directory:
        work = Path(directory)
        # STR r0,[r1]; LDR r2,[r1]; B .  No vendor firmware is loaded.
        stub = struct.pack('<3I', 0xe5810000, 0xe5912000, 0xeafffffe)
        globals_ = ('nikon-z72-boot-config.sd-host=on',) if hosts else ()
        devices = ()
        extra_arguments = ()
        if card_engine:
            image = work / 'card.raw'
            with image.open('wb') as output:
                output.truncate(1024 * 1024)
            extra_arguments = ('-drive',
                f'if=none,id=card,file={image},format=raw,snapshot=on')
            devices = (f'sd-card,drive=card,bus=/z72-sd-{card_engine}/sd-bus',)

        def launch(command, **kwargs):
            # Keep the existing dual-machine setup/connection helper; only
            # select the board variant for this synthetic fixture.
            command = command.copy()
            command[command.index('-M') + 1] = f'nikon-{board}-dual-lab'
            return subprocess.Popen(command, **kwargs)

        process = remote = None
        try:
            process, remote = dual_machine(
                qemu, work, stub, stub, work / 'console.txt',
                globals_=globals_, devices=devices,
                extra_arguments=extra_arguments, process_factory=launch,
                icount=None, trace=None)

            def access(thread, address, value=None):
                remote.select_thread(thread)
                remote.set(pc=BASE + (4 if value is None else 0), cpsr=0xd3,
                           r0=value or 0, r1=address)
                remote.command(f'vCont;s:{thread:x}')
                regs = remote.regs()
                require(remote.get(regs, 'pc') ==
                        BASE + (8 if value is None else 4),
                        f'{label}: GPIO access did not execute')
                return remote.get(regs, 'r2')

            def expected(value, inserted):
                if board == 'z62':
                    return (value & ~DETECT) | (0 if inserted else DETECT)
                return value

            stored = {1: 0, 3: 0}
            inserted = card_engine is not None
            for thread in stored:
                require(access(thread, GPIO) == expected(0, inserted),
                        f'{label}: incorrect initial detect on CPU {thread}')
            for thread in stored:
                for pattern in (0, 0xffffffff, 0xa55a25a5, 0x5aa5da5a):
                    access(thread, GPIO, pattern)
                    stored[thread] = pattern
                    for cpu, value in stored.items():
                        require(access(cpu, GPIO) == expected(value, inserted),
                                f'{label}: detect/neighbor bits differ on CPU {cpu}')
                access(thread, GPIO - 4, 0x7fed1234)
                require(access(thread, GPIO - 4) == 0x7fed1234,
                        f'{label}: adjacent GPIO word changed')

            if card_engine:
                # Query the bus on each read rather than caching attachment
                # at realize: removal must immediately reach both engines.
                monitor(remote, 'eject -f card')
                for thread, value in stored.items():
                    require(access(thread, GPIO) == expected(value, False),
                            f'{label}: removal not reflected on CPU {thread}')
                    access(thread, GPIO, 0)
                    require(access(thread, GPIO) == expected(0, False),
                            f'{label}: guest write forged an inserted card')
            print(f'PASS {label}: shared detect, GPIO preservation and guest writes')
        finally:
            if remote is not None:
                remote.sock.close()
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def run(qemu):
    for board in ('z62', 'z72'):
        for hosts, card_engine in ((False, None), (True, None),
                                   (True, 'slave'), (True, 'master')):
            check(qemu, board, hosts, card_engine)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    run(parser.parse_args().qemu)
