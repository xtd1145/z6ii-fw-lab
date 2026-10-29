# SPDX-License-Identifier: GPL-2.0-or-later
"""Owned original-firmware QEMU session for the NikonUnlock handler lab.

No USB/WPD access. Request files expose only explicit lab operations.
Native ROM callbacks run in an independently scheduled instrumentation task.
USB/session observers are bypassed. No real camera is accessed.
"""
import os
import argparse
import json
import importlib
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import connect, free_port, initialize_inputs, monitor, qtest, sample
from tools import z62_ptp_lab

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--session', type=Path, required=True, help='New test directory')
parser.add_argument('--baseline', type=Path,
                    default=ROOT / '.local/z62-native-menu-reproduction')
parser.add_argument('--nor', type=Path, required=True, help='Original 64 MiB NOR dump')
parser.add_argument('--nor-store', type=Path, help='Writable private NOR copy; never the original dump')
parser.add_argument('--card', type=Path, required=True, help='Private FAT32 image, snapshot overlay used')
parser.add_argument('--nvme-card', type=Path, help='Optional private A-slot NVMe image')
parser.add_argument('--laboratory-pcie-transfer-ready', action='store_true',
                    help='Opt-in hypothesis for unknown 2B200210 input; recorded in command.json')
parser.add_argument('--watch', action='append', default=[], type=lambda value: int(value, 0),
                    help='Native observation breakpoint after lab task installation')
parser.add_argument('--boot-watch', action='append', default=[], type=lambda value: int(value, 0),
                    help='Early read-only observation before the PTP task exists')
parser.add_argument('--qemu', type=Path, help='Optional QEMU binary override')
parser.add_argument('--lifetime', type=int, default=3600)
parser.add_argument('--transaction-gap-ms', type=int, default=0,
                    help='Original RTOS delay between transport requests (timing fixture)')
args = parser.parse_args()
directory = args.session.resolve()
directory.mkdir(parents=True, exist_ok=False)
(directory / 'requests').mkdir()
baseline = args.baseline.resolve()
command = json.loads((baseline / 'command.json').read_text())
command = [os.devnull if v == "/dev/null" else v for v in command]
if args.qemu:
    command[0] = str(args.qemu.resolve())
for name in ('resident.bin', 'primary.bin', 'auxiliary.bin'):
    shutil.copy2(baseline / name, directory / name)
    command = [value.replace(str(baseline / name), str(directory / name)) for value in command]
nor = args.nor.resolve()
if args.nor_store and args.nor_store.resolve() == nor:
    raise ValueError('The original NOR dump cannot be the writable private store')
if nor.stat().st_size != 0x4000000:
    raise ValueError('Expected the original 64 MiB NOR image')
with nor.open('rb') as source:
    source.seek(0x2a20000)
    group8 = source.read(0x20000)
assert len(group8) == 0x20000 and group8[-4:] == b'ENDM'
(directory / 'group8.bin').write_bytes(group8)
index = command.index('-M') + 1
(directory / 'group17-18.bin').write_bytes((baseline / 'group17.bin').read_bytes() +
                                          (baseline / 'group18.bin').read_bytes())
start = command[index].index('master-boot-aux=')
end = command[index].index(',controller-flash-data=', start)
auxiliary = (f'master-boot-aux={directory}/group8.bin@0x12a20000,, '
             f'{directory}/group17-18.bin@0x132c0000,, '
             f'{baseline}/group32.bin@0x13e80000').replace(',, ', ',,')
if args.nor_store:
    # The real NOR contains group8, including prior native commits. Never
    # restore it from the original dump when restarting this experiment.
    auxiliary = (f'master-boot-aux={directory}/group17-18.bin@0x132c0000,, '
                 f'{baseline}/group32.bin@0x13e80000').replace(',, ', ',,')
command[index] = command[index][:start] + auxiliary + command[index][end:]
gdb_port, wire_port = free_port(), free_port()
command[command.index('-gdb') + 1] = f'tcp:127.0.0.1:{gdb_port}'
command[command.index('-qtest') + 1] = f'tcp:127.0.0.1:{wire_port},server=on,wait=off'
card = args.card.resolve()
command += ['-global', 'nikon-z72-boot-config.sd-host=on',
    '-drive', f'if=none,id=unlock-card,file={card},format=raw,snapshot=on',
    '-device', 'sd-card,drive=unlock-card,bus=/z72-sd-slave/sd-bus']
if args.nvme_card:
    command += ['-global', 'nikon-z72-boot-config.master-nvme-host=on',
        '-drive', f'if=none,id=unlock-nvme,file={args.nvme_card.resolve()},format=raw,snapshot=on',
        '-device', 'nvme,drive=unlock-nvme,serial=Z62QEMU0001,bus=dw-pcie,max_ioqpairs=2,msix_qsize=3']
if args.laboratory_pcie_transfer_ready:
    command += ['-global', 'nikon-z72-boot-config.laboratory-pcie-transfer-ready=on']
if args.nor_store:
    command += ['-global', 'nikon-z72-boot-config.master-parallel-nor=on',
        '-drive', f'if=pflash,index=0,file={args.nor_store.resolve()},format=raw']
if os.environ.get('Z62_NVME_TRACE') == '1':
    command += ['-trace', 'enable=pci_nvme_*', '-D', str(directory/'nvme.trace')]
(directory / 'command.json').write_text(json.dumps(command, indent=2))

def ticks():
    remote.select_thread(1)
    return int.from_bytes(remote.read(0x04266008, 8), 'little') / 48000000

def run(guest, host=60):
    initial = ticks()
    deadline = time.monotonic() + host
    stop = None
    while ticks() - initial < guest and time.monotonic() < deadline:
        remote.sock.settimeout(2)
        try:
            stop = remote.command('vCont;c')
            break
        except TimeoutError:
            remote.sock.settimeout(15)
            remote.interrupt()
    remote.sock.settimeout(15)
    return dict(guest_delta=ticks() - initial, virtual_seconds=ticks(), stop_reply=stop)

with (directory / 'qemu.log').open('wb') as log:
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
    (directory / 'runtime.json').write_text(json.dumps(dict(
        qemu_pid=process.pid, started_unix=time.time(),
        nvme_trace=os.environ.get("Z62_NVME_TRACE")=="1",
        nor_store=str(args.nor_store.resolve()) if args.nor_store else None,
        transaction_gap_ms=args.transaction_gap_ms,
        laboratory_pcie_transfer_ready=args.laboratory_pcie_transfer_ready), indent=2))
    remote = wire = None
    try:
        remote = connect(process, gdb_port, directory / 'qemu.log')
        wire = socket.create_connection(('127.0.0.1', wire_port), timeout=10)
        initialize_inputs(remote, wire, True)
        for vector in (0x10100004, 0x1010000c, 0x10100010):
            remote.breakpoint(vector)
        remote.breakpoint(0x1048c38e)
        for address in args.boot_watch:
            remote.breakpoint(address)
        startup = run(3, 90)
        if startup['stop_reply']:
            remote.select_thread(z62_ptp_lab.stopped_thread(startup['stop_reply']))
            registers = remote.regs()
            startup['registers'] = {name: hex(remote.get(registers, name)) for name in
                                    ('pc', 'lr', 'sp', 'cpsr', *[f'r{i}' for i in range(13)])}
            startup['faults'] = {name: hex(remote.read_register(name)) for name in
                                 ('IFSR_S', 'IFAR_S', 'DFSR_S', 'DFAR_S')}
        (directory / 'startup.json').write_text(json.dumps(startup))
        lab = None
        if startup.get('registers', {}).get('pc') == hex(z62_ptp_lab.PTP_WAIT):
            try:
                lab = z62_ptp_lab.install(remote, z62_ptp_lab.stopped_thread(startup['stop_reply']), directory)
                lab['transaction_gap_ms'] = args.transaction_gap_ms
                (directory / 'guest-task.json').write_text(json.dumps(lab, indent=2))
                print('TASK', json.dumps(lab), flush=True)
                for address in args.watch:
                    remote.breakpoint(address)
                (directory / 'after-task-start.json').write_text(json.dumps(run(3, 90)))
            except Exception as error:
                (directory / 'task-install-error.json').write_text(json.dumps(dict(error=repr(error))))
                raise
        else:
            print('READ_ONLY: original PTP task has not reached its installation boundary', flush=True)
        print('READY', directory, flush=True)
        ptp = z62_ptp_lab.NativePtp(remote, lab, directory) if lab else None
        done = set()
        deadline = time.monotonic() + args.lifetime
        while time.monotonic() < deadline:
            requests = sorted(path for path in (directory / 'requests').glob('*.json') if path.name not in done)
            if not requests:
                time.sleep(0.1)
                continue
            for path in requests:
                try:
                    job = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue
                result = {}
                stop = False
                try:
                    if job['op'] == 'run':
                        result = run(job['guest'], job.get('host', 60))
                    elif job['op'] == 'read':
                        remote.select_thread(job.get('thread', 1))
                        result = {address: remote.read(int(address, 0), count).hex()
                                  for address, count in job['blocks'].items()}
                    elif job['op'] == 'registers':
                        remote.select_thread(job.get('thread', 1))
                        registers = remote.regs()
                        result = {name: hex(remote.get(registers, name)) for name in
                                  ('pc', 'lr', 'sp', 'cpsr', *[f'r{i}' for i in range(13)])}
                        result.update({name: hex(remote.read_register(name)) for name in
                                       ('IFSR_S', 'IFAR_S', 'DFSR_S', 'DFAR_S')})
                    elif job['op'] == 'breakpoint':
                        for address in job.get('add', []): remote.breakpoint(int(address, 0))
                        for address in job.get('remove', []): remote.breakpoint(int(address, 0), False)
                    elif job['op'] == 'event':
                        for level in job.get('levels', [1, 0]):
                            qtest(wire, f"set_irq_in {job['path']} {job['input']} {job['line']} {level}")
                    elif job['op'] == 'observe':
                        folder = directory / path.stem
                        folder.mkdir()
                        result = sample(remote, folder)
                        monitor(remote, 'screendump ' + str(folder / 'lcd.ppm'))
                    elif job['op'] == 'native-call':
                        importlib.reload(z62_ptp_lab)
                        result = dict(value=z62_ptp_lab.invoke(remote, lab, int(job['function'], 0),
                            job.get('arguments', [])))
                    elif job['op'] == 'install-rpc':
                        if lab is not None:
                            raise ValueError('Laboratory task already installed')
                        lab = z62_ptp_lab.install(remote, job['thread'], directory)
                        lab['transaction_gap_ms'] = args.transaction_gap_ms
                        ptp = z62_ptp_lab.NativePtp(remote, lab, directory)
                        result = lab
                    elif job['op'] == 'path-exists':
                        importlib.reload(z62_ptp_lab)
                        native_path = job['path'].encode('ascii') + b'\0'
                        if len(native_path) > 128 or native_path[:3] not in (b'A:\\', b'B:\\'):
                            raise ValueError('Only laboratory A/B absolute paths')
                        remote.select_thread(1)
                        remote.write(lab['packet'], native_path)
                        result = dict(path=job['path'], exists=z62_ptp_lab.invoke(
                            remote, lab, 0x104d69ea, [lab['packet']]))
                    elif job['op'] == 'ptp':
                        importlib.reload(z62_ptp_lab)
                        ptp.__class__ = z62_ptp_lab.NativePtp
                        result = ptp.transaction(int(job['opcode'], 0), job.get('parameters', []))
                    elif job['op'] == 'stop':
                        stop = True
                    else:
                        raise ValueError(job['op'])
                except Exception as error:
                    result = dict(error=repr(error))
                result_path = directory / (path.stem + '-result.json')
                temporary = result_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(result, indent=2))
                temporary.replace(result_path)
                done.add(path.name)
                print('DONE', path.stem, json.dumps(result)[:180], flush=True)
                if stop: break
            if stop: break
    finally:
        if wire: wire.close()
        if remote: remote.sock.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
