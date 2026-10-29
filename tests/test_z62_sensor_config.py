#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise Z62 sensor configuration through real guest load/store instructions."""
import os
import argparse
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import connect, monitor, free_port, qtest


def check(qemu, completion_source=False):
    with tempfile.TemporaryDirectory(prefix='z62-sensor-') as directory:
        folder = Path(directory)
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
        remote = None
        wire = None
        qtest_port = free_port()
        with (folder / 'qemu.log').open('wb') as log:
            process = subprocess.Popen([str(qemu), '-M', 'nikon-z62-dual-lab',
                '-smp', '4', '-display', 'none', '-serial', 'none', '-monitor',
                'none', '-nic', 'none', '-S', '-gdb', f'tcp:127.0.0.1:{port}',
                '-qtest', f'tcp:127.0.0.1:{qtest_port},server=on,wait=off',
                '-qtest-log', os.devnull, '-global',
                'nikon-z62-sensor-config.laboratory-init-completion=' +
                ('on' if completion_source else 'off'), '-global',
                'nikon-z72-input-config.laboratory-metadata-completion=' +
                ('on' if completion_source else 'off'), '-global',
                'nikon-z62-sensor-config.laboratory-mode-completion=' +
                ('on' if completion_source else 'off'), '-global',
                'nikon-z62-sensor-config.laboratory-frame-completion=' +
                ('on' if completion_source else 'off')],
                stdout=subprocess.DEVNULL, stderr=log)
            try:
                remote = connect(process, port, folder / 'qemu.log')
                wire = socket.create_connection(('127.0.0.1', qtest_port), timeout=10)
                base = 0x10020000
                code = struct.pack('<4I', 0xe5810000, 0xe5912000, 0xe5c10000, 0xeafffffe)
                for thread in (1, 3):
                    remote.select_thread(thread)
                    remote.write(base, code)

                def access(thread, address, value=None, byte=False, valid=True):
                    remote.select_thread(thread)
                    instruction = 2 if byte else 1 if value is None else 0
                    remote.set(pc=base + instruction * 4, cpsr=0xd3,
                               r0=value or 0, r1=address)
                    remote.command(f'vCont;s:{thread:x}')
                    regs = remote.regs()
                    if valid:
                        assert remote.get(regs, 'pc') == base + (instruction + 1) * 4, hex(address)
                    else:
                        assert remote.get(regs, 'pc') == 0x10, hex(address)
                        assert remote.get(regs, 'cpsr') & 0x1f == 0x17
                    return remote.get(regs, 'r2')

                for thread, peer in ((1, 3), (3, 1)):
                    for address in (0x20064004, 0x20064100, 0x20065000,
                                    0x20065104, 0x2006510c, 0x20065400,
                                    0x20065504, 0x2006550c, 0x20065550):
                        previous = access(peer, address)
                        value = 0x12345600 + thread
                        access(thread, address, value)
                        assert access(thread, address) == value
                        assert access(peer, address) == previous
                    for address in (0x20064014, 0x20065024, 0x20065108,
                                    0x20065110, 0x20065118, 0x2006511c,
                                    0x20065424, 0x20065508, 0x20065510,
                                    0x20065518, 0x2006551c,
                                    0x20064028, 0x20065144, 0x20065148,
                                    0x20065544, 0x20065548):
                        access(thread, address, 0xffffffff)
                        assert access(thread, address) == 0, hex(address)
                    for address in (0x2006400c, 0x20064020, 0x20065010,
                                    0x20065100, 0x2006554c, 0x20065600):
                        access(thread, address, 1, valid=False)
                    access(thread, 0x2006550c, 1, byte=True, valid=False)
                    access(thread, 0x2006550d, 1, valid=False)
                groups = ((0x14, 0x10, 89), (0x1108, 0x110c, 90),
                          (0x1024, 0x1028, 91), (0x1110, 0x1114, 91),
                          (0x1118, 0x1120, 91), (0x111c, 0x1124, 91),
                          (0x1508, 0x150c, 92), (0x1424, 0x1428, 93),
                          (0x1510, 0x1514, 93), (0x1518, 0x1520, 93),
                          (0x151c, 0x1524, 93))
                def signal(thread, name, line, value):
                    role = 'master' if thread == 1 else 'slave'
                    qtest(wire, f'set_irq_in /machine/{role}-sensor {name} {line} {value}')

                def pending(thread, irq):
                    return bool(access(thread, 0x04001200 + irq // 32 * 4) & (1 << (irq % 32)))

                def clear_pending(thread, irq):
                    access(thread, 0x04001280 + irq // 32 * 4, 1 << (irq % 32))

                for thread in (1, 3):
                    for status, mask, irq in groups:
                        access(thread, 0x20064000 + mask, 0)
                        access(thread, 0x20064000 + status, 0xffffffff)
                        clear_pending(thread, irq)
                for thread, peer in ((1, 3), (3, 1)):
                    for bank, (status, mask, irq) in enumerate(groups):
                        status += 0x20064000
                        mask += 0x20064000
                        bit = 1 << 31
                        line = bank * 32 + 31
                        signal(thread, 'sensor-event', line, 1)
                        assert access(thread, status) == bit and not pending(thread, irq)
                        access(thread, mask, bit)
                        assert pending(thread, irq) and not pending(peer, irq)
                        assert access(peer, status) == 0
                        access(thread, status, 1)
                        assert access(thread, status) == bit
                        access(thread, mask, 0)
                        clear_pending(thread, irq)
                        assert not pending(thread, irq) and access(thread, status) == bit
                        access(thread, mask, bit)
                        assert pending(thread, irq)
                        access(thread, status, bit)
                        clear_pending(thread, irq)
                        signal(thread, 'sensor-event', line, 1)
                        assert access(thread, status) == 0 and not pending(thread, irq)
                        signal(thread, 'sensor-event', line, 0)
                        access(thread, mask, 0)
                    # Several status banks share an IRQ: acknowledging one
                    # cause cannot lower the line while the other remains.
                    access(thread, 0x20065120, 1)
                    access(thread, 0x20065124, 1)
                    signal(thread, 'sensor-event', 4 * 32, 1)
                    signal(thread, 'sensor-event', 5 * 32, 1)
                    access(thread, 0x20065118, 1)
                    clear_pending(thread, 91)
                    assert pending(thread, 91)
                    access(thread, 0x2006511c, 1)
                    clear_pending(thread, 91)
                    assert not pending(thread, 91)
                    for index, address in enumerate((0x20064028, 0x20065144,
                            0x20065148, 0x20065544, 0x20065548)):
                        value = 0x12340000 + index + thread
                        peer_value = access(peer, address)
                        signal(thread, 'sensor-result', index, value)
                        assert access(thread, address) == value
                        access(thread, address, 0xffffffff)
                        assert access(thread, address) == value
                        assert access(peer, address) == peer_value
                    # Result snapshots never raise a completion by themselves.
                    assert all(not pending(thread, irq) for irq in range(89, 94))
                # The actual scatter-clear target that previously faulted.
                access(3, 0xc6307240, 0x89abcdef)
                assert access(3, 0xc6307240) == 0x89abcdef
                # The next original fault used VA341F5400, whose original
                # section descriptor 8C814426 resolves to this physical pool.
                access(1, 0x8c8f5400, 0x13579bdf)
                assert access(1, 0x8c8f5400) == 0x13579bdf
                access(1, 0x341f5400, 1, valid=False)
                # Original Z62 1020B130 writes a 129-entry table, using
                # 32-bit stores even though the source entries are u16.
                for thread, peer in ((1, 3), (3, 1)):
                    for index in range(129):
                        address = 0x2006b000 + index * 4
                        before = access(peer, address)
                        access(thread, address, thread * 0x1000 + index)
                        assert access(thread, address) == thread * 0x1000 + index
                        assert access(peer, address) == before
                    access(thread, 0x2006b204, 1, valid=False)
                    access(thread, 0x2006b000, 1, byte=True, valid=False)
                    for index in range(93):
                        address = 0x2006a200 + index * 4
                        before = access(peer, address)
                        access(thread, address, thread * 0x1000 + index)
                        assert access(thread, address) == thread * 0x1000 + index
                        assert access(peer, address) == before
                    access(thread, 0x2006a374, 1, valid=False)
                    for address in range(0x2006a3e0, 0x2006a3f0, 4):
                        access(thread, address, 0xffffffff)
                        assert access(thread, address) == 0
                    for instance in (0x2006a000, 0x2006be00):
                        for offset in (*range(0, 0x28, 4), 0x40):
                            address = instance + offset
                            previous = access(peer, address)
                            access(thread, address, 0x12340000 + thread + offset)
                            assert access(thread, address) == 0x12340000 + thread + offset
                            assert access(peer, address) == previous
                        for offset in (0x44, *range(0x100, 0x130, 4)):
                            access(thread, instance + offset, 0xffffffff)
                            assert access(thread, instance + offset) == 0
                        for offset in (0x28, 0x3c, 0x48, 0xfc, 0x130):
                            access(thread, instance + offset, 1, valid=False)
                    for address in (*range(0x2006b800, 0x2006b988, 4),
                            *range(0x2006ba00, 0x2006ba54, 4),
                            *range(0x2006ba94, 0x2006babc, 4),
                            0x2006bb18, 0x2006bb24, 0x2006bb3c, 0x2006bb44,
                            0x2006bb48, 0x2006bb4c, 0x2006bb50, 0x2006bb60,
                            0x2006bb80, 0x2006bc50, 0x2006bc60, 0x2006bc80,
                            0x2006bd50, 0x2006bd60, 0x2006bd80):
                        previous = access(peer, address)
                        access(thread, address, 0x12340000 + thread)
                        assert access(thread, address) == 0x12340000 + thread
                        assert access(peer, address) == previous
                    for address in (0x2006b988, *range(0x2006bb70, 0x2006bb80, 4),
                            0x2006bc78, 0x2006bc7c, 0x2006bd78, 0x2006bd7c):
                        access(thread, address, 0xffffffff)
                        assert access(thread, address) == 0
                    access(thread, 0x2006ba54, 1, valid=False)
                monitor(remote, 'system_reset')
                for thread in (1, 3):
                    assert access(thread, 0x2006550c) == 0
                    assert access(thread, 0x20065104) == 0
                    assert access(thread, 0x20065544) == 0
                    assert access(thread, 0x2006b000) == 0
                    assert access(thread, 0x2006b200) == 0
                    assert all(not pending(thread, irq) for irq in range(89, 94))
                    access(thread, 0x20065120, 1)
                    signal(thread, 'sensor-event', 4 * 32, 1)
                    assert access(thread, 0x20065118) == 1 and pending(thread, 91)
                monitor(remote, 'system_reset')

                def advance():
                    for thread in range(1, 5):
                        remote.select_thread(thread)
                        remote.set(pc=base + 12, cpsr=0xd3)
                    remote.sock.settimeout(.12)
                    try:
                        remote.command('vCont;c')
                        raise AssertionError('Unexpected synthetic guest stop')
                    except TimeoutError:
                        remote.sock.settimeout(15)
                        remote.interrupt()

                # Ordinary configuration, other commands, unarmed consumer,
                # and another mode must not manufacture init completion.
                access(1, 0x20065400, 0x10)
                access(1, 0x20068104, 1)  # no consumer mask yet
                advance()
                assert access(1, 0x20065508) == 0
                access(1, 0x2006550c, 2)
                access(1, 0x20065504, 0x4000)
                access(1, 0x20068104, 1)
                advance()
                assert access(1, 0x20065508) == 0
                access(1, 0x20065504, 0)
                access(1, 0x20068104, 2)
                access(1, 0x20068100, 1)
                advance()
                assert access(1, 0x20065508) == 0
                access(1, 0x20065400, 0)
                access(1, 0x20068104, 1)
                advance()
                assert access(1, 0x20065508) == 0
                access(1, 0x20065400, 0x10)
                access(1, 0x20068104, 1)
                assert access(1, 0x20065508) == 0, 'Completion was not delayed'
                advance()
                assert access(1, 0x20065508) == (2 if completion_source else 0)
                assert pending(1, 92) == completion_source and not pending(3, 92)
                assert access(1, 0x20065544) == 0
                access(1, 0x20065508, 2)
                clear_pending(1, 92)
                access(1, 0x20068104, 1)
                advance()
                assert access(1, 0x20065508) == 0, 'Init source repeated completion'
                monitor(remote, 'system_reset')
                access(1, 0x20065400, 0x10)
                access(1, 0x2006550c, 2)
                access(1, 0x20068104, 1)
                monitor(remote, 'system_reset')
                advance()
                assert access(1, 0x20065508) == 0 and not pending(1, 92)

                # A bank1 mode0 write has its own single response, gated by
                # bit4 configuration and an armed sync bit10 consumer. Other
                # modes, cancellation, held configuration and reset cannot
                # manufacture a periodic acknowledgement.
                for thread, peer in ((1, 3), (3, 1)):
                    access(thread, 0x20065400, 0x10)
                    access(thread, 0x20065504, 0)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20060040, 0x400)
                    access(thread, 0x20065400, 0)
                    access(thread, 0x20065504, 0)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065400, 0x10)
                    access(thread, 0x20065504, 0x8000)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    for _ in range(2):
                        access(thread, 0x20065504, 0)
                        advance()
                        assert access(thread, 0x20060044) == (0x400 if completion_source else 0)
                        assert pending(thread, 87) == completion_source
                        assert access(peer, 0x20060044) == 0 and not pending(peer, 87)
                        access(thread, 0x20060044, 0x400)
                        clear_pending(thread, 87)
                        advance()
                        assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065504, 0)
                    access(thread, 0x20065504, 0x4000)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065504, 0)
                    access(thread, 0x20060040, 0)
                    advance()
                    access(thread, 0x20060040, 0x400)
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065504, 0)
                    access(thread, 0x20065400, 0)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20060040, 0)
                monitor(remote, 'system_reset')

                # The running-mode2 request needs a later command101
                # doorbell. Mode write, wrong command and duplicate submit
                # cannot substitute for this distinct sync-bit18 response.
                for thread, peer in ((1, 3), (3, 1)):
                    access(thread, 0x20060040, 0x40000)
                    access(thread, 0x20065400, 0x10)
                    access(thread, 0x20065504, 0x8000)
                    access(thread, 0x200632e0, 0x101)
                    access(thread, 0x200632e4, 1)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065400, 0x10011)
                    access(thread, 0x20065504, 0x8000)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x200632e0, 0x100)
                    access(thread, 0x200632e4, 1)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x200632e0, 0x101)
                    access(thread, 0x200632e4, 1)
                    assert access(thread, 0x20060044) == 0
                    advance()
                    assert access(thread, 0x20060044) == (0x40000 if completion_source else 0)
                    assert access(peer, 0x20060044) == 0
                    access(thread, 0x20060044, 0x40000)
                    clear_pending(thread, 87)
                    access(thread, 0x200632e4, 1)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20065504, 0x8000)
                    access(thread, 0x200632e4, 1)
                    access(thread, 0x20060040, 0)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20060040, 0x40000)
                    advance()
                    assert access(thread, 0x20060044) == 0
                    access(thread, 0x20060040, 0)
                monitor(remote, 'system_reset')

                # Sensor-path completion is gated by running mode2, the
                # original IRQ92 mask and an external frame edge.
                for thread, peer, name in ((1, 3, 'master'), (3, 1, 'slave')):
                    path = f'/machine/{name}-sensor'
                    def frame(level):
                        qtest(wire, f'set_irq_in {path} frame-end 0 {level}')
                    access(thread, 0x20065400, 0x10)
                    access(thread, 0x20065504, 0x8000)
                    access(thread, 0x2006550c, 2)
                    frame(1); frame(0)
                    assert access(thread, 0x20065508) == 0
                    access(thread, 0x20065400, 0x10011)
                    access(thread, 0x2006550c, 0)
                    frame(1); frame(0)
                    assert access(thread, 0x20065508) == 0
                    access(thread, 0x2006550c, 2)
                    for _ in range(2):
                        frame(1)
                        assert access(thread, 0x20065508) == (2 if completion_source else 0)
                        assert access(peer, 0x20065508) == 0
                        access(thread, 0x20065508, 2)
                        clear_pending(thread, 92)
                        frame(1)
                        assert access(thread, 0x20065508) == 0
                        frame(0)
                    access(thread, 0x2006550c, 0)
                monitor(remote, 'system_reset')

                # A frame-end completion is an explicit laboratory source,
                # gated by both mode-0 input enable bits. Configuration alone
                # cannot raise it, and no pixel/result data is fabricated.
                for thread, role, peer in ((1, 'master', 3), (3, 'slave', 1)):
                    def frame(level):
                        qtest(wire, f'set_irq_in /machine/{role}-input frame-end 0 {level}')
                    access(thread, 0x20068200, 0x40)
                    for control in (0, 1, 0x10):
                        access(thread, 0x20069f00, control)
                        frame(1)
                        frame(0)
                        assert access(thread, 0x20068210) & 0x40 == 0
                    access(thread, 0x20069f00, 0x11)
                    assert access(thread, 0x20068210) & 0x40 == 0
                    frame(1)
                    assert bool(access(thread, 0x20068210) & 0x40) == completion_source
                    assert pending(thread, 94) == completion_source
                    assert not pending(peer, 94)
                    access(thread, 0x20068210, 0x40)
                    clear_pending(thread, 94)
                    frame(1)
                    assert access(thread, 0x20068210) & 0x40 == 0
                    frame(0)
                    assert access(thread, 0x2006b988) == 0
                    access(thread, 0x20069f00, 0)
                monitor(remote, 'system_reset')
                assert monitor(remote, 'qom-get /machine/master-input metadata-completions').strip() == '0'
                print('PASS Z62 sensor configuration: original fault target, engine isolation, '
                      '11 event banks, shared IRQ89..93, retained masked events, edge latches, '
                      'W1C, external read-only results, rejected holes/access widths, reset and slave scatter RAM; '
                      'optional init/mode/sensor-frame/metadata completions ' + ('ON' if completion_source else 'OFF'))
            finally:
                if wire:
                    wire.close()
                if remote:
                    remote.sock.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--completion-source', action='store_true')
    args = parser.parse_args()
    check(args.qemu, args.completion_source)
