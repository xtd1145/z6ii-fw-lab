#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot verified Z6 II C1.70 images and record read-only runtime evidence."""
import os
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze.qemu_display import Remote
from tools.graphics_boot_probe import graphics_observation

IDENTITIES = {
    'master': (38750138, 'bb83613121f843d65101da936d942acdc513d86dbd3abc26259d0d870d03f5d4'),
    'slave': (8832194, '3fd8c2cbdf57fadb03f6d69b596b58a8703efdd61ae82572d5afeea0241a9ed5'),
    'controller': (524290, '9c65cad554ed02f83d37fbc825a8eb9efe79e3f32f98894dd5dd73edee416983'),
}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def nor_parameters(source, destination):
    """Read real groups selected by the Z62 ROM table at 12400210."""
    if source.stat().st_size != 0x4000000:
        raise ValueError('Expected the complete 64 MiB NOR dump')
    groups = []
    with source.open('rb') as stream:
        for number, address, size in ((17, 0x132c0000, 0x20000),
                (18, 0x132e0000, 0xa0000), (32, 0x13e80000, 0x180000)):
            stream.seek(address - 0x10000000)
            data = stream.read(size)
            if len(data) != size or data[-4:] != b'ENDM':
                raise ValueError(f'Incomplete NOR group {number}')
            path = destination / f'group{number}.bin'
            path.write_bytes(data)
            groups.append(f'{path}@{address:#x}')
    return ','.join(groups).replace(',', ',,')


def laboratory_devices(destination):
    """Explicit bench inputs; these are not recovered controller flash."""
    resident = bytearray(0xc000)
    resident[:4] = b'MAKE'
    (destination / 'resident.bin').write_bytes(resident)
    primary = bytearray(256)
    primary[11] = 1
    (destination / 'primary.bin').write_bytes(primary)
    auxiliary = bytearray(256)
    auxiliary[9], auxiliary[12] = 8, 0xc9
    (destination / 'auxiliary.bin').write_bytes(auxiliary)
    devices = [
        'nikon-z72-controller-register-bank,bus=controller-i2c0,address=79',
        'nikon-z72-controller-expander,bus=controller-i2c0,address=34,id=keys,'
        'interrupt-gpio=/machine/controller/keys-gpio,interrupt-pin=8',
        'nikon-z72-controller-register-bank,bus=controller-i2c0,address=107,'
        f'initial-state={destination}/primary.bin',
        'nikon-z72-controller-register-bank,bus=controller-i2c0,address=13,'
        f'initial-state={destination}/auxiliary.bin']
    devices += [f'nikon-z72-controller-register-bank,bus=master-i2c{i},address=104'
                for i in range(4)]
    return devices


def qtest(wire, request):
    wire.sendall((request + '\n').encode())
    response = bytearray()
    while not response.endswith(b'\n'):
        part = wire.recv(1024)
        if not part:
            raise RuntimeError('QTest connection closed')
        response.extend(part)
    if response.strip() != b'OK':
        raise RuntimeError(f'QTest {request}: {response!r}')


def initialize_inputs(remote, wire, frame_sync=False):
    listing = monitor(remote, 'qom-list /machine/unattached')
    def instances(kind):
        names = re.findall(r'(device\[\d+\]) \(child<' + kind + r'>\)', listing)
        return sorted(names, key=lambda name: int(re.search(r'\d+', name)[0]))
    gpios = instances('nikon-z72-controller-gpio')
    if len(gpios) != 8:
        raise RuntimeError('Expected eight controller GPIO banks')
    # Power/run inputs and released shutter stages. Defaults hold G14/G15 low.
    for bank, pin, level in ((2, 5, 0), (3, 13, 1), (4, 9, 1),
                             (6, 7, 1), (1, 11, 1), (6, 14, 1), (6, 15, 1)):
        qtest(wire, f'set_irq_in /machine/unattached/{gpios[bank]} '
                    f'unnamed-gpio-in {pin} {level}')
    if frame_sync:
        configs = instances('nikon-z72-boot-config')
        if len(configs) != 2:
            raise RuntimeError('Expected two engine synchronization receivers')
        for level in (0, 1):
            qtest(wire, f'set_irq_in /machine/unattached/{configs[0]} '
                        f'frame-sync-enable 0 {level}')


def monitor(remote, command):
    response = remote.command('qRcmd,' + command.encode().hex())
    result = bytearray()
    while response.startswith('O') and response != 'OK':
        result.extend(bytes.fromhex(response[1:]))
        response = remote.receive()
    if response != 'OK':
        raise RuntimeError('Monitor failed: ' + command + ' -> ' + response)
    return result.decode()


def connect(process, port, log):
    deadline = time.monotonic() + 20
    while True:
        try:
            return Remote(port)
        except ConnectionRefusedError:
            if process.poll() is not None:
                raise RuntimeError('QEMU exited: ' + log.read_text(errors='replace'))
            if time.monotonic() > deadline:
                raise TimeoutError('QEMU debugger connection timed out')
            time.sleep(0.05)


def sample(remote, destination):
    cores = []
    for thread in range(1, 6):
        remote.select_thread(thread)
        regs = remote.regs()
        core = {'thread': thread, 'pc': hex(remote.get(regs, 'pc')),
                'cpsr': hex(remote.get(regs, 'cpsr'))}
        if thread <= 4:
            core['faults'] = {name: remote.read_register(name) for name in
                             ('DFAR_S', 'IFAR_S', 'DFSR_S', 'IFSR_S')}
        cores.append(core)
    remote.select_thread(1)
    manager = remote.read(0xc1811e50, 88)
    foreground = dict(address='0xc1811e50',
        initialized=struct.unpack_from('<I', manager)[0],
        notified=struct.unpack_from('<I', manager, 4)[0],
        current_index=manager[16], next_index=manager[17], state=manager[24],
        applications=[])
    for index, name in enumerate(('APPIDLE', 'APPMENU', 'APPPLAY', 'APPLV', 'APPMSG')):
        getter, context, ready = struct.unpack_from('<3I', manager, 28 + index * 12)
        foreground['applications'].append(dict(index=index, name=name,
            getter=hex(getter), context=hex(context), ready=ready))
    # Independently located in the Z62 image: mode writer 1020D384,
    # DSID 1921 descriptor 11913E54, and switch decoder 10436600.
    mode = remote.read(0xc1818d34, 8)
    status = remote.read(0xc253e0c0, 0x118)
    count = struct.unpack_from('<I', status, 0x110)[0]
    codes = list(struct.unpack_from('<' + 'I' * count, status)) if count <= 68 else None
    switches = struct.unpack('<3I', remote.read(0xc25d6fc0, 12))
    resource = struct.unpack('<I', remote.read(0xc18a77ac, 4))[0]
    system = dict(mode_context=hex(struct.unpack_from('<I', mode)[0]),
        current_mode=mode[4], previous_mode=mode[5],
        power_input=struct.unpack('<b', remote.read(0xc1818ed4, 1))[0],
        transition_inhibitors=list(struct.unpack('<3I', remote.read(0xc1818f00, 12))),
        deferred_flags=struct.unpack('<I', remote.read(0xc1818f0c, 4))[0],
        deferred_target=remote.read(0xc1818f14, 1)[0],
        status_count=count, status_codes=codes,
        menu_blocking_codes=[code for code in codes if 0x3e <= code <= 0x44]
            if codes is not None else None,
        switch_words=[hex(word) for word in switches],
        menu_pressed=bool(switches[1] & 0x8000), resource_pointer=hex(resource),
        resource_version=remote.read(resource + 0xce014, 2).hex() if resource else None)
    # 10242D8E iterates eleven SPI records, reading FIFO capacity at
    # [record+1C]+48. This was incorrectly called an application table in
    # the supplied handoff; +20 is not an application workspace size.
    records = remote.read(0xc1802090, 11 * 0x58)
    spi = []
    for index in range(11):
        at = index * 0x58
        spi.append(dict(channel=index,
            **{name: struct.unpack_from('<I', records, at + offset)[0]
               for name, offset in (('word0', 0), ('enabled', 4), ('word8', 8),
                                    ('mmio_base', 0x1c), ('fifo_depth', 0x20),
                                    ('word28', 0x28), ('word34', 0x34))}))
    service_data = remote.read(0xc18ac5c0, 160 * 28)
    services = []
    for at in range(0, len(service_data), 28):
        name = service_data[at:at + 8].split(b'\0', 1)[0]
        if name and all(32 <= byte <= 126 for byte in name):
            services.append(dict(name=name.decode('ascii'),
                                 id=struct.unpack_from('<H', service_data, at + 10)[0]))
    (destination / 'core-64k.bin').write_bytes(remote.read(0xc1800000, 65536))
    lcd = remote.read(0x20140000, 0xc000)
    (destination / 'lcd-registers.bin').write_bytes(lcd)
    output = {hex(offset): hex(struct.unpack_from('<I', lcd, 0x4000 + offset)[0])
              for offset in (0x110, 0x200, 0x204, 0x210, 0x220, 0x240,
                             0x250, 0x260, 0x1100, 0x1200, 0x1210, 0x1214)}
    return dict(cores=cores, foreground=foreground, system=system, spi_channels=spi, services=services,
                graphics=graphics_observation(remote), lcd=output,
                lcd_scanned_frames=json.loads(monitor(remote,
                    'qom-get /machine/z62-lcd scanned-frames')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    for role in IDENTITIES:
        parser.add_argument('--' + role, required=True, type=Path)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--nor', type=Path, help='Read groups 17/18/32 from a real 64 MiB NOR dump')
    parser.add_argument('--sd-card', type=Path,
                        help='Attach a raw SD image to the slave host using a temporary write overlay')
    parser.add_argument('--laboratory-inputs', action='store_true',
                        help='Supply explicit bench power, I2C peers and released switches')
    parser.add_argument('--frame-sync', action='store_true',
                        help='Enable the experimental periodic sensor-sync source')
    parser.add_argument('--frame-start', action='store_true',
                        help='Add normal bank2 bit7 boundaries to the periodic sync source')
    parser.add_argument('--sensor-init-completion', action='store_true',
                        help='Opt into the bounded experimental configuration completion source')
    parser.add_argument('--frame-metadata-completion', action='store_true',
                        help='Opt into metadata completion at each configured mode0 frame end; no pixels')
    parser.add_argument('--input-command-completion', action='store_true',
                        help='Acknowledge armed original input stop/drain commands; no pixel DMA')
    parser.add_argument('--sensor-mode-completion', action='store_true',
                        help='Acknowledge armed bank1 mode0 writes and subsequent mode2 submissions')
    parser.add_argument('--sensor-frame-completion', action='store_true',
                        help='Supply armed running-sensor completions at frame ends; no pixels')
    parser.add_argument('--menu', action='store_true',
                        help='After startup, press and release the original controller MENU input')
    parser.add_argument('--key-window', type=float, default=20,
                        help='Host seconds to observe each pressed/released phase (default: 20)')
    parser.add_argument('--navigation-check', action='store_true',
                        help='After MENU, move a row and switch to the next menu category')
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error('--seconds must be within 0..300')
    if args.frame_sync and not args.laboratory_inputs:
        parser.error('--frame-sync requires --laboratory-inputs')
    if args.frame_start and not args.frame_sync:
        parser.error('--frame-start requires --frame-sync')
    if args.sensor_init_completion and not args.laboratory_inputs:
        parser.error('--sensor-init-completion requires --laboratory-inputs')
    if args.frame_metadata_completion and not args.frame_start:
        parser.error('--frame-metadata-completion requires --frame-start')
    if args.input_command_completion and not args.laboratory_inputs:
        parser.error('--input-command-completion requires --laboratory-inputs')
    if args.sensor_mode_completion and not args.laboratory_inputs:
        parser.error('--sensor-mode-completion requires --laboratory-inputs')
    if args.sensor_frame_completion and not args.frame_start:
        parser.error('--sensor-frame-completion requires --frame-start')
    if args.menu and not args.laboratory_inputs:
        parser.error('--menu requires --laboratory-inputs')
    if args.navigation_check and not args.menu:
        parser.error('--navigation-check requires --menu')
    if args.sd_card is not None:
        args.sd_card = args.sd_card.resolve()
        if not args.sd_card.is_file():
            parser.error('--sd-card must name an existing raw card image')
    if not 0 < args.key_window <= 60:
        parser.error('--key-window must be within 0..60')
    for role, (size, sha) in IDENTITIES.items():
        path = getattr(args, role).resolve()
        if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            parser.error(role + ' must be the verified Z6 II C1.70 image')
        setattr(args, role, path)
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    port = free_port()
    machine = ('nikon-z62-dual-lab,controller-firmware=' + str(args.controller) +
               ',controller-link=on,controller-battery-peer=on,master-boot-image=' +
               str(args.master) + ',slave-boot-image=' + str(args.slave))
    if args.nor:
        machine += ',master-boot-aux=' + nor_parameters(args.nor.resolve(), destination)
    devices = laboratory_devices(destination) if args.laboratory_inputs else []
    if args.laboratory_inputs:
        machine += f',controller-flash-data={destination}/resident.bin'
    command = [str(args.qemu.resolve()), '-M', machine, '-smp', '4,maxcpus=5',
               '-display', 'none', '-serial', 'none', '-monitor', 'none', '-nic', 'none',
               '-S', '-gdb', f'tcp:127.0.0.1:{port}', '-icount', 'shift=0,sleep=off']
    if args.sd_card is not None:
        command += ['-global', 'nikon-z72-boot-config.sd-host=on',
            '-drive', f'if=none,id=z62-card,file={args.sd_card},format=raw,snapshot=on',
            '-device', 'sd-card,drive=z62-card,bus=/z72-sd-slave/sd-bus']
    for device in devices:
        command += ['-device', device]
    if args.laboratory_inputs:
        qtest_port = free_port()
        command += ['-qtest', f'tcp:127.0.0.1:{qtest_port},server=on,wait=off',
                    '-qtest-log', os.devnull]
        for channel, value in ((0, 1800), (1, 3001), (3, 3001)):
            command += ['-global', f'nikon-z72-controller-adc.channel{channel}={value}']
    if args.frame_sync:
        command += ['-global', 'nikon-z72-boot-config.frame-sync=on']
    if args.frame_start:
        command += ['-global', 'nikon-z72-boot-config.frame-start=on']
    if args.sensor_init_completion:
        command += ['-global', 'nikon-z62-sensor-config.laboratory-init-completion=on']
    if args.frame_metadata_completion:
        command += ['-global', 'nikon-z72-input-config.laboratory-metadata-completion=on']
    if args.input_command_completion:
        command += ['-global', 'nikon-z72-boot-config.input-command-completion=on']
    if args.sensor_mode_completion:
        command += ['-global', 'nikon-z62-sensor-config.laboratory-mode-completion=on']
    if args.sensor_frame_completion:
        command += ['-global', 'nikon-z62-sensor-config.laboratory-frame-completion=on']
    for cpu in range(4):
        command += ['-device', f'loader,addr=0x10020000,cpu-num={cpu}']
    (destination / 'command.json').write_text(json.dumps(command, indent=2), encoding='utf-8')
    log = destination / 'qemu.log'
    remote = wire = None
    with log.open('wb') as output:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=output)
        try:
            remote = connect(process, port, log)
            if args.laboratory_inputs:
                wire = socket.create_connection(('127.0.0.1', qtest_port), timeout=10)
                initialize_inputs(remote, wire, args.frame_sync)
            def run_window(seconds):
                remote.sock.settimeout(seconds)
                try:
                    remote.command('vCont;c')
                except TimeoutError:
                    remote.sock.settimeout(15)
                    remote.interrupt()

            observations = []
            def observe(label):
                folder = destination / label
                folder.mkdir()
                snapshot = sample(remote, folder)
                snapshot['virtual_seconds'] = int.from_bytes(
                    remote.read(0x04266008, 8), 'little') / 48000000
                monitor(remote, 'screendump ' + str(folder / 'lcd.ppm'))
                (folder / 'report.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
                observations.append(dict(label=label, foreground=snapshot['foreground'],
                    system=snapshot['system'], graphics=snapshot['graphics'],
                    lcd_scanned_frames=snapshot['lcd_scanned_frames'],
                    virtual_seconds=snapshot['virtual_seconds']))
                print(label, 'foreground=', snapshot['foreground']['current_index'],
                      'frames=', snapshot['lcd_scanned_frames'], flush=True)

            print('Running verified Z62 firmware for', args.seconds, 'host seconds', flush=True)
            run_window(args.seconds)
            if args.menu:
                observe('before-menu')
                for label, level in (('menu-held', 0), ('menu-released', 1)):
                    qtest(wire, f'set_irq_in /machine/peripheral/keys pins 9 {level}')
                    run_window(args.key_window)
                    observe(label)
            if args.navigation_check:
                def ticks():
                    # An interrupt may stop on the controller. The clock
                    # belongs to an engine address space, so select it.
                    remote.select_thread(1)
                    return int.from_bytes(remote.read(0x04266008, 8), 'little')

                def virtual_window(seconds):
                    deadline = ticks() + int(seconds * 48000000)
                    while ticks() < deadline:
                        run_window(1)

                # Original Z62 switch names at 10938C04/10938C2C confirm
                # pin6=RIGHT, pin7=LEFT; the old Z72 web labels were reversed.
                for label, pin in (('row-down', 4), ('category-left', 7),
                                   ('category-down', 4), ('category-right', 6)):
                    for level in (0, 1):
                        qtest(wire, f'set_irq_in /machine/peripheral/keys pins {pin} {level}')
                        virtual_window(0.15)
                    virtual_window(0.2)
                    observe(label)
            result = sample(remote, destination)
            screenshot = destination / 'lcd.ppm'
            monitor(remote, 'screendump ' + str(screenshot))
            result['observation_seconds'] = args.seconds
            result['laboratory_inputs'] = args.laboratory_inputs
            result['periodic_sync_enabled'] = args.frame_sync
            result['periodic_frame_start_enabled'] = args.frame_start
            result['experimental_sensor_init_completion'] = args.sensor_init_completion
            result['experimental_frame_metadata_completion'] = args.frame_metadata_completion
            result['experimental_input_command_completion'] = args.input_command_completion
            result['experimental_sensor_mode_completion'] = args.sensor_mode_completion
            result['experimental_sensor_frame_completion'] = args.sensor_frame_completion
            result['nor_parameters_source'] = str(args.nor.resolve()) if args.nor else None
            result['menu_input_requested'] = args.menu
            result['navigation_check_requested'] = args.navigation_check
            result['observations'] = observations
            result['original_menu_application_active'] = result['foreground']['current_index'] == 1
            result['virtual_seconds'] = int.from_bytes(remote.read(0x04266008, 8), 'little') / 48000000
            result['exception_registers_clear'] = all(
                not core['faults']['DFSR_S'] and not core['faults']['IFSR_S']
                for core in result['cores'][:4])
            result['lcd_scanout_observed'] = result['lcd_scanned_frames'] > 0
            (destination / 'report.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps({key: result[key] for key in
                ('graphics', 'lcd_scanned_frames', 'exception_registers_clear')}, indent=2), flush=True)
            print('Report:', destination / 'report.json', flush=True)
        finally:
            if wire is not None:
                wire.close()
            if remote is not None:
                remote.sock.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
