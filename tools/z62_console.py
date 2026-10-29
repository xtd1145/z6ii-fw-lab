#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Local, interactive console for the verified Z6 II C1.70 machine."""
import os
import argparse
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze.decode_z72 import require
from tools import web_console as shared
from tools.z62_probe import (IDENTITIES, connect, initialize_inputs,
                             laboratory_devices)

WEB = ROOT / 'web-z62'
MANAGER = 0xc1811e50
GROUPS = ((17, 0x132c0000, 0x20000), (18, 0x132e0000, 0xa0000),
          (32, 0x13e80000, 0x180000))
PACKAGE_VERSION = '2026-10-28-r3.1'


class Z62Machine(shared.Machine):
    """Reuse monitor, lifecycle and key ownership; keep Z62 boot/state separate."""
    def _start(self):
        args = self.args
        self.experimental_boundaries = 0
        self.experimental_boundary_tick = 0
        self.experimental_poll_ticks = 0
        for role, (size, digest) in IDENTITIES.items():
            path = getattr(args, role)
            require(path.stat().st_size == size and
                    hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                    f'{role}: expected the original Z62 C1.70 image')
        for group, _, size in GROUPS:
            data = (args.parameters / f'group{group}.bin').read_bytes()
            require(len(data) == size and data[-4:] == b'ENDM',
                    f'Incomplete parameter group {group}')
        args.runs.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='z62-', dir=args.runs))
        work = self.work
        gdb_port, wire_port, qmp_port = (shared.free_port() for _ in range(3))
        auxiliary = ','.join(f'{args.parameters}/group{group}.bin@{address:#x}'
                             for group, address, _ in GROUPS).replace(',', ',,')
        devices = laboratory_devices(work)
        machine = (f'nikon-z62-dual-lab,controller-firmware={args.controller},'
                   f'controller-link=on,controller-battery-peer=on,'
                   f'master-boot-image={args.master},slave-boot-image={args.slave},'
                   f'master-boot-aux={auxiliary},controller-flash-data={work}/resident.bin')
        command = [str(args.qemu), '-M', machine, '-smp', '4,maxcpus=5',
                   '-display', 'none', '-serial', 'none', '-monitor', 'none',
                   '-nic', 'none', '-S', '-gdb', f'tcp:127.0.0.1:{gdb_port}',
                   '-icount', 'shift=0,sleep=off',
                   '-qtest', f'tcp:127.0.0.1:{wire_port},server=on,wait=off',
                   '-qtest-log', os.devnull,
                   '-qmp', f'tcp:127.0.0.1:{qmp_port},server=on,wait=off',
                   '-global', 'nikon-z72-boot-config.frame-sync=on']
        if args.sd_card is not None:
            require(args.sd_card.is_file(), 'SD card image does not exist')
            command += ['-global', 'nikon-z72-boot-config.sd-host=on',
                '-drive', f'if=none,id=z62-card,file={args.sd_card},format=raw,snapshot=on',
                '-device', 'sd-card,drive=z62-card,bus=/z72-sd-slave/sd-bus']
        if args.main_screen_experiment:
            # Replay the two external decoded boundaries used by session20.
            # This is a bench fixture, not recovered finite-capture timing.
            for prop in (
                'nikon-z72-boot-config.frame-start=on',
                'nikon-z72-input-config.laboratory-metadata-completion=on',
                'nikon-z72-boot-config.input-command-completion=on',
                'nikon-z62-sensor-config.laboratory-mode-completion=on',
                'nikon-z62-sensor-config.laboratory-frame-completion=on',
                'nikon-z62-sensor-config.laboratory-init-completion=on',
                'nikon-z62-sensor-config.init-delay-ns=250000000',
            ):
                command += ['-global', prop]
        for device in devices:
            command += ['-device', device]
        for channel, value in ((0, 1800), (1, 3001), (3, 3001)):
            command += ['-global', f'nikon-z72-controller-adc.channel{channel}={value}']
        for cpu in range(4):
            command += ['-device', f'loader,addr=0x10020000,cpu-num={cpu}']
        (work / 'command.json').write_text(json.dumps(command, indent=2), encoding='utf-8')
        log = work / 'console.txt'
        with log.open('wb') as stream:
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stream)
        self.remote = connect(self.process, gdb_port, log)
        self.wire = socket.create_connection(('127.0.0.1', wire_port), timeout=5)
        initialize_inputs(self.remote, self.wire, frame_sync=True)
        self.wire_file = self.wire.makefile('rwb')
        self.qmp = shared.QmpClient(qmp_port)
        require(self.remote.command('D') == 'OK', 'Could not start Z62')
        self.remote.sock.close()
        self.remote = None
        self.started_at = time.monotonic()
        self.frame_interval = 0.2
        self.firmware = dict(model='Z6 II', version='C1.70', source='original')

    def _set_key_locked(self, pin, level):
        events = self.key_events.setdefault(pin, deque())
        previous = events[-1] if events else int(pin not in self.held)
        if previous != level:
            events.append(level)
        if not events:
            del self.key_events[pin]
        if level == 0:
            self.requested_keys.add(pin)
            self.key_deadline = time.monotonic() + 5
        else:
            self.requested_keys.discard(pin)
        self._advance_keys()

    def _advance_keys(self):
        if not self.key_events:
            return
        ticks, frequency = self._key_clock()
        for pin, events in tuple(self.key_events.items()):
            if ticks < self.next_key_ticks.get(pin, 0):
                continue
            level = events.popleft()
            self._qtest(f'set_irq_in /machine/peripheral/keys pins {pin} {level}')
            if level == 0:
                self.held.add(pin)
            else:
                self.held.discard(pin)
            if not events:
                del self.key_events[pin]
            self.next_key_ticks[pin] = ticks + (frequency * 15 + 99) // 100

    def _auto_menu_step(self):
        if self.args.main_screen_experiment:
            self._main_screen_experiment_step()
        if not self.auto_menu or self.auto_menu_done:
            return
        self.auto_menu_ticks += 1
        if self.auto_menu_ticks % 6:
            return
        try:
            words = self._words(MANAGER, 5)
            ticks, frequency = self._key_clock()
        except (OSError, ValueError, RuntimeError):
            return
        self.app_index = words[4] & 255
        if words[0] == 1 and self.app_index == 3 and ticks >= frequency * 3:
            self.auto_menu_done = True
            self._set_key_locked(9, 0)
            self._set_key_locked(9, 1)

    def _main_screen_experiment_step(self):
        if self.experimental_boundaries >= 2:
            return
        self.experimental_poll_ticks += 1
        if self.experimental_poll_ticks % 6:
            return
        flags = self._words(0xc269fa0c, 1)[0]
        pending = self._words(0xc269fa1c, 1)[0]
        mode = self._words(0xc1888084, 1)[0] >> 24
        armed = self._words(0x20068260, 1)[0] & 0x80
        if not (flags & 4 and mode == 1 and armed and
                self._words(0xc1868490, 1)[0] == 4):
            return
        ticks, frequency = self._key_clock()
        if self.experimental_boundaries == 0:
            if pending or not self._words(0x20068008, 1)[0] & 0x10:
                return
            if not self._words(0x2006a580, 1)[0]:
                return
        elif pending != 1 or ticks - self.experimental_boundary_tick < frequency * 0.15:
            return
        for level in (1, 0):
            self._qtest('set_irq_in /machine/unattached/device[3] '
                        f'input-event 103 {level}')
        self.experimental_boundaries += 1
        self.experimental_boundary_tick = ticks
        print('Bench capture boundary', self.experimental_boundaries, '/ 2', flush=True)

    def _menu_hold_step(self):
        # Z62 consumes a debounced press/release. No application-state writes.
        pass

    def status(self):
        state = dict(model='Z62', phase=self.phase, error=self.error, ready=False,
                     package_version=PACKAGE_VERSION, package_root=str(ROOT.parent),
                     main_screen_experiment=self.args.main_screen_experiment,
                     sd_card_attached=self.args.sd_card is not None,
                     experimental_boundaries=getattr(self, 'experimental_boundaries', 0),
                     input_ready=False, main_screen_usable=False,
                     auto_menu='off' if not self.auto_menu else
                         'pressed' if self.auto_menu_done else 'armed',
                     uptime=int(time.monotonic() - self.started_at) if self.started_at else 0)
        if self.phase != 'running':
            return state
        with self.io:
            if self.phase != 'running':
                return dict(state, phase=self.phase)
            if self.process.poll() is not None:
                return dict(state, phase='failed', error='模拟器已退出，请重启')
            state['execution'] = self.qmp.command('query-status')['status']
            words = self._words(MANAGER, 5)
            state.update(ready=words[0] == 1, index=words[4] & 255,
                         held=sorted(self.held), queued=sorted(self.key_events))
            self.app_index = state['index']
            state['input_ready'] = state['ready'] and state['index'] in (1, 2, 3, 4)
            # APPLV queues a MENU request until its normal event49 dispatch.
            # Observe that request so the UI does not suggest restarting while
            # the original application is still processing the transition.
            state['menu_pending'] = (state['index'] == 3 and
                                     self._words(0xc181dae8, 1)[0] >> 16 == 2)
            ticks, frequency = self._key_clock()
            state['virtual_seconds'] = round(ticks / frequency, 3)
            state['lcd_frames'] = self.qmp.command('qom-get',
                {'path': '/machine/z62-lcd', 'property': 'scanned-frames'})
            state['display_enabled'] = bool(self._words(0xc1d195b4, 1)[0] & 1 and
                                           self._words(0x201465c0, 1)[0] & 1)
        return state

    def _cleanup(self):
        if self.work is not None:
            root, work = self.args.runs.resolve(), self.work.resolve()
            require(work.is_relative_to(root) and work != root, 'Unexpected session directory')
            for name in ('console.txt', 'command.json'):
                if (work / name).exists():
                    shutil.copyfile(work / name, root / ('last-' + name))
        super()._cleanup()


def make_handler(machine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data, kind='application/json; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return self.headers.get('Host') in hosts and self.headers.get('Origin',
                'http://' + self.headers.get('Host', '')) in {'http://' + host for host in hosts}

        def do_GET(self):
            if not self.valid_host():
                self.send(403, b'{}')
                return
            try:
                if self.path == '/status':
                    self.send(200, json.dumps(machine.status(), ensure_ascii=False).encode())
                elif self.path == '/frame':
                    self.send(200, machine.frame_png(), 'image/png')
                elif self.path in ('/', '/console.js', '/style.css'):
                    name = 'index.html' if self.path == '/' else self.path[1:]
                    kind = 'text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css'
                    self.send(200, (WEB / name).read_bytes(), kind + '; charset=utf-8')
                else:
                    self.send(404, b'{}')
            except Exception as error:
                self.send(503, json.dumps(dict(error=str(error)), ensure_ascii=False).encode())

        def do_POST(self):
            if not self.valid_host():
                self.send(403, b'{}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                require(self.headers.get_content_type() == 'application/json' and 0 < length <= 4096,
                        'Expected a small JSON request')
                request = json.loads(self.rfile.read(length))
                require(isinstance(request, dict), 'Expected an object')
                if self.path == '/key':
                    machine.set_key(request.get('pin'), request.get('level'))
                elif self.path == '/release':
                    machine.release()
                elif self.path == '/heartbeat':
                    machine.heartbeat()
                elif self.path == '/auto-menu':
                    machine.set_auto_menu(request.get('enabled'))
                elif self.path == '/restart':
                    require(machine.restart(), '正在重启，请稍候')
                elif self.path == '/stop':
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.send(404, b'{}')
                    return
                self.send(200, b'{"ok":true}')
            except Exception as error:
                self.send(400, json.dumps(dict(error=str(error)), ensure_ascii=False).encode())
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    for role in IDENTITIES:
        parser.add_argument('--' + role, type=Path, required=True)
    parser.add_argument('--parameters', type=Path, required=True)
    parser.add_argument('--runs', type=Path, default=ROOT / '.local' / 'z62-console')
    parser.add_argument('--port', type=int, default=8662)
    parser.add_argument('--no-auto-menu', action='store_true')
    parser.add_argument('--main-screen-experiment', action='store_true',
                        help='Replay the two external capture boundaries from the main-screen experiment')
    parser.add_argument('--sd-card', type=Path,
                        help='Attach a raw SD image to the slave host; guest writes use a temporary overlay')
    args = parser.parse_args()
    for name in ('qemu', 'master', 'slave', 'controller', 'parameters', 'runs'):
        setattr(args, name, getattr(args, name).resolve())
    if args.sd_card is not None:
        args.sd_card = args.sd_card.resolve()
    require(1 <= args.port <= 65535, 'Invalid port')
    args.auto_menu = not (args.no_auto_menu or args.main_screen_experiment)
    args.wsl = False
    machine = Z62Machine(args)
    server = None
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(machine))
        machine.restart()
        print(f'Z62 console: http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        machine.close()


if __name__ == '__main__':
    main()
