# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise PCIe config, real NVMe queues, DMA and backing sectors; no firmware."""
import os
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import struct

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--qemu', required=True)
parser.add_argument('--report', type=Path)
parser.add_argument('--empty-slot', action='store_true')
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='z62-nvme-') as folder:
    image = Path(folder) / 'card.raw'
    with image.open('wb') as stream:
        stream.write(bytes(range(256)) * 2)
        stream.truncate(8 * 1024**2)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = [args.qemu,
        '-M', 'nikon-z72-lab', '-accel', 'qtest', '-display', 'none',
        '-serial', 'none', '-monitor', 'none', '-nic', 'none',
        '-global', 'nikon-z72-boot-config.master-nvme-host=on',
        '-global', 'nikon-z72-boot-config.laboratory-pcie-transfer-ready=on',
        '-drive', f'if=none,id=card,file={image},format=raw,snapshot=on',
        '-device', 'nvme,drive=card,serial=Z62QEMU0001,bus=dw-pcie,max_ioqpairs=2,msix_qsize=3',
        '-qtest', f'tcp:127.0.0.1:{port},server=on,wait=off', '-qtest-log', os.devnull]
    if args.empty_slot:
        first = command.index('-drive')
        del command[first:first+4]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    wire = None
    try:
        deadline = time.monotonic() + 10
        while wire is None:
            try:
                wire = socket.create_connection(('127.0.0.1', port), timeout=2)
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode())
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        stream = wire.makefile('rwb')
        def q(text, raw=False):
            stream.write(text.encode() + b'\n')
            stream.flush()
            reply = stream.readline().decode().split()
            assert reply[0] == 'OK', reply
            return (reply[1] if raw else int(reply[1], 0)) if len(reply) > 1 else None
        def write(address, value): q(f'writel {address:#x} {value:#x}')
        def read(address): return q(f'readl {address:#x}')
        if args.empty_slot:
            for pattern in (0, 4, 0xffffffff):
                write(0x2b000004, pattern)
                assert read(0x2b000010) & 0x400 == 0
            print('PASS empty NVMe slot: guest clock writes cannot forge an attached card')
            raise SystemExit(0)
        def atu(index, base, limit, target, kind):
            for offset, value in ((0, index), (0xc, base), (0x10, 0),
                    (0x14, limit), (0x18, target), (0x1c, 0), (4, kind), (8, 0x80000000)):
                write(0x2a900900 + offset, value)
        atu(0, 0x2a800000, 0x2a80ffff, 0, 4)
        result = dict(root_id=hex(read(0x2a900000)), endpoint_id=hex(read(0x2a800000)),
                      endpoint_class=hex(read(0x2a800008)))
        assert read(0x2a800008) >> 8 == 0x010802
        assert read(0x2b000010) & 0x400
        write(0x2b000004, 4)
        assert read(0x2b000010) & 0x400, 'Clock request hid a physically attached A card'
        assert read(0x2a900098) == 0
        write(0x2a900098, 2)
        assert read(0x2a900098) == 2, 'Native root field did not preserve guest write'
        write(0x2a800010, 0x2a810000)
        write(0x2a800014, 0)
        write(0x2a800004, 0x106)
        atu(1, 0x2a810000, 0x2a8fffff, 0x2a810000, 0)
        write(0x2a900004, 0x106)
        result['bar0'] = hex(read(0x2a800010))
        result['cap_before_bridge_window'] = hex(read(0x2a810000))
        assert read(0x2a810000) != 0, 'iATU aperture incorrectly depended on bridge header window'
        result['bridge_memory_window'] = hex(read(0x2a900020))
        write(0x2a900020, 0x2a802a80)
        write(0x2a900004, 0x106)
        result['root_command'] = hex(read(0x2a900004))
        result['endpoint_command'] = hex(read(0x2a800004))
        result['bridge_memory_after'] = hex(read(0x2a900020))
        result['viewport'] = [hex(read(0x2a900900+i)) for i in range(0, 0x20, 4)]
        result['cap_after_bridge_window'] = hex(read(0x2a810000))
        result['version'] = hex(read(0x2a810008))
        assert read(0x2a810000) != 0
        assert read(0x2b200210) == 0
        write(0x2b200210, 0xffffffff)
        assert read(0x2b200210) == 0, 'Guest write forged experimental readiness'
        def put(address, data): q(f'write {address:#x} {len(data):#x} 0x{data.hex()}')
        def get(address, size): return bytes.fromhex(q(f'read {address:#x} {size:#x}', True)[2:])
        for address in range(0xb2000000, 0xb2006000, 0x1000):
            put(address, bytes(0x1000))
        bar = 0x2a810000
        write(bar+0x24, 0x00010001)
        for offset, value in ((0x28, 0xb2000000), (0x2c, 0),
                              (0x30, 0xb2001000), (0x34, 0)):
            write(bar+offset, value)
        write(bar+0x14, 0x00460001)
        for _ in range(100):
            q('clock_step 1000000')
            if read(bar+0x1c) & 1: break
        assert read(bar+0x1c) & 1, 'NVMe controller never became ready'
        assert read(0x2b200210) == 1
        write(0x2a800004, 0)
        assert read(0x2b200210) == 0, 'Experimental readiness ignored endpoint disable'
        write(0x2a800004, 0x106)
        states = {0: dict(sq=0xb2000000, cq=0xb2001000, tail=0, head=0, phase=1),
                  1: dict(sq=0xb2004000, cq=0xb2003000, tail=0, head=0, phase=1)}
        cid = 0
        def submit(qid, opcode, nsid=0, data=0, cdw10=0, cdw11=0):
            global cid
            cid += 1
            state = states[qid]
            entry = bytearray(64)
            struct.pack_into('<II', entry, 0, opcode | cid << 16, nsid)
            struct.pack_into('<Q', entry, 0x18, data)
            struct.pack_into('<II', entry, 0x28, cdw10, cdw11)
            put(state['sq'] + state['tail']*64, entry)
            state['tail'] ^= 1
            write(bar+0x1000+qid*8, state['tail'])
            for _ in range(200):
                q('clock_step 1000000')
                completion = get(state['cq'] + state['head']*16, 16)
                actual_cid, status = struct.unpack_from('<HH', completion, 12)
                if actual_cid == cid and status & 1 == state['phase']: break
            else: raise AssertionError(f'No DMA completion for opcode {opcode:x}')
            interrupt_before_ack = read(0x2b200024) & (1 << 28)
            if interrupt_before_ack:
                write(0x2b200024, 1 << 28)
                assert read(0x2b200024) & (1 << 28) == 0
                assert get(state['cq'] + state['head']*16, 16) == completion
            state['head'] ^= 1
            if not state['head']: state['phase'] ^= 1
            write(bar+0x1004+qid*8, state['head'])
            return status >> 1, bool(interrupt_before_ack)
        status, interrupt = submit(0, 6, data=0xb2002000, cdw10=1)
        assert status == 0
        identify = get(0xb2002000, 4096)
        assert identify[4:24].rstrip() == b'Z62QEMU0001'
        assert interrupt, 'Real admin completion did not assert PCIe INTA'
        assert read(0x2b200024) & (1 << 28) == 0, 'CQ acknowledgement left INTA high'
        status, _ = submit(0, 6, nsid=1, data=0xb2002000)
        assert status == 0
        assert struct.unpack_from('<Q', get(0xb2002000, 4096))[0] == 16384
        assert submit(0, 5, data=0xb2003000, cdw10=0x10001, cdw11=3)[0] == 0
        assert submit(0, 1, data=0xb2004000, cdw10=0x10001, cdw11=0x10001)[0] == 0
        assert submit(0, 0xff)[0] != 0, 'Unknown opcode incorrectly succeeded'
        status, interrupt = submit(1, 2, nsid=1, data=0xb2005000)
        assert status == 0 and interrupt
        assert get(0xb2005000, 512) == bytes(range(256))*2
        assert read(0x2b200024) & (1 << 28) == 0
        result.update(identify_controller=True, identify_namespace=True,
                      actual_sector_dma=True, queue_phase_wrap=True,
                      invalid_opcode_rejected=True, intx_acknowledgement=True,
                      layer='synthetic hardware protocol fixture; no vendor firmware')
        if args.report:
            args.report.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if wire:
            wire.close()
        process.terminate()
        process.wait(timeout=5)
