# SPDX-License-Identifier: GPL-2.0-or-later
"""Native reset alias, no-preload guard and NOR persistence across reset."""
import os
import argparse
import importlib.util
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.z62_probe import connect,free_port
spec=importlib.util.spec_from_file_location('boot_store_test',ROOT/'tests/test_boot_store.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--qemu',required=True)
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='z62-native-nor-') as temporary:
    folder=Path(temporary)
    image=folder/'nor.raw'
    code=struct.pack('<6I',0xe59f0008,0xe59f1008,0xe5810000,0xeafffffe,0x12345678,0xc2000100)
    with image.open('wb') as output:
        for _ in range(64): output.write(b'\xff'*1024**2)
    with image.open('r+b') as output: output.write(code)
    gdb,qmp,qtest=free_port(),free_port(),free_port()
    command=[args.qemu,'-M','nikon-z62-dual-lab,native-nor-boot=on','-smp','4',
        '-display','none','-serial','none','-monitor','none','-nic','none','-S',
        '-gdb',f'tcp:127.0.0.1:{gdb}',
        '-qmp',f'tcp:127.0.0.1:{qmp},server=on,wait=off',
        '-qtest',f'tcp:127.0.0.1:{qtest},server=on,wait=off','-qtest-log',os.devnull,
        '-drive',f'if=pflash,index=0,file={image},format=raw']
    remote=monitor=wire=None
    with (folder/'qemu.log').open('wb') as log:
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=log)
        try:
            remote=connect(process,gdb,folder/'qemu.log')
            monitor=module.Monitor(qmp)
            monitor.command('qmp_capabilities')
            remote.select_thread(1)
            assert remote.get(remote.regs(),'pc')==0
            assert remote.read(0,len(code))==remote.read(0x10000000,len(code))==code
            cpus=monitor.command('query-cpus-fast')
            for cpu in cpus:
                off=monitor.command('qom-get',{'path':cpu['qom-path'],'property':'start-powered-off'})
                assert off==(cpu['cpu-index']>=2)
            remote.breakpoint(12)
            remote.sock.settimeout(10)
            remote.command('vCont;c')
            remote.select_thread(1)
            assert remote.read(0xc2000100,4)==struct.pack('<I',0x12345678)
            for thread in (3,4):
                remote.select_thread(thread)
                assert remote.get(remote.regs(),'pc')==0
            wire=socket.create_connection(('127.0.0.1',qtest),timeout=5)
            stream=wire.makefile('rwb')
            def q(command):
                stream.write(command.encode()+b'\n');stream.flush()
                reply=stream.readline().decode().split()
                assert reply[0]=='OK',reply
                return int(reply[1],0) if len(reply)>1 else None
            for bank in (0x2002e000,0x2004e000):
                assert q(f'readl {bank+0x308:#x}')==0
                q(f'writel {bank+0x308:#x} 1')
                assert q(f'readl {bank+0x308:#x}')==0,'Software forged hardware ready'
                q(f'writel {bank+0x300:#x} 0x10001')
                assert q(f'readl {bank+0x300:#x}')==0,'Status write forged completion'
                q(f'writel {bank+0x108:#x} 1')
                assert q(f'readl {bank+0x308:#x}')&1
                q(f'writel {bank+0x100:#x} 0')
                assert q(f'readl {bank+0x300:#x}')==0x10000
                assert q(f'readl {bank+0x308:#x}')==0
                q(f'writel {bank+0x300:#x} 0x10000')
                assert q(f'readl {bank+0x300:#x}')==0
                q(f'writel {bank+0x100:#x} 1')
                assert q(f'readl {bank+0x300:#x}')==1
            for control in (0x20028010,0x20028810,0x20028c10,0x20048010):
                status=control+0x20
                q(f'writel {status:#x} 0x10001')
                assert q(f'readl {status:#x}')==0
                q(f'writel {control:#x} 0')
                assert q(f'readl {status:#x}')==0x10000
                q(f'writel {status:#x} 0x10000')
                assert q(f'readl {status:#x}')==0
                q(f'writel {control:#x} 1')
                assert q(f'readl {status:#x}')==1
            for address,value in ((0x10000aaa,0xaa),(0x10000554,0x55),
                                  (0x10000aaa,0xa0),(0x10020000,0xbeef)):
                q(f'writew {address:#x} {value:#x}')
            assert q('readw 0x10020000')==0xbeef
            monitor.command('stop')
            monitor.command('system_reset')
            remote.select_thread(1)
            assert remote.get(remote.regs(),'pc')==0
            assert remote.read(0,len(code))==code
            assert q('readw 0x10020000')==0xbeef,'Reset replayed MAIN over saved NOR'
            for property,value in (('image',str(image)),('auxiliary',str(image)+'@0x10020000'),('harvest-update',True)):
                try: monitor.command('qom-set',{'path':'/machine/master-boot-store','property':property,'value':value})
                except RuntimeError as error: assert 'forbids' in str(error)
                else: raise AssertionError('Native mode accepted '+property)
        finally:
            if wire: wire.close()
            if monitor: monitor.sock.close()
            if remote: remote.sock.close()
            process.terminate();process.wait(timeout=5)
    bad=subprocess.run([args.qemu,'-M','nikon-z62-dual-lab,native-nor-boot=on,master-boot-image='+str(image),
        '-smp','4','-display','none','-serial','none','-monitor','none','-nic','none'],capture_output=True,text=True)
    assert bad.returncode and 'forbids all boot image' in bad.stderr
    with image.open('rb') as source:
        source.seek(0x20000)
        assert source.read(2)==bytes.fromhex('efbe')
    # Diagnostic SLA execution must remain explicit while MAS still resets
    # through NOR. It is a separate fixture, never a native-SLA success.
    slave=folder/'slave-fixture.bin'
    slave.write_bytes(struct.pack('<6I',0xe59f0008,0xe59f1008,0xe5810000,0xeafffffe,0x87654321,0xc1000100))
    port=free_port()
    diagnostic=[args.qemu,'-M','nikon-z62-dual-lab,native-nor-boot=on,slave-runtime-fixture='+str(slave),
        '-smp','4','-display','none','-serial','none','-monitor','none','-nic','none','-S',
        '-gdb',f'tcp:127.0.0.1:{port}','-drive',f'if=pflash,index=0,file={image},format=raw']
    with (folder/'diagnostic.log').open('wb') as log:
        process=subprocess.Popen(diagnostic,stdout=subprocess.DEVNULL,stderr=log)
        remote=None
        try:
            remote=connect(process,port,folder/'diagnostic.log')
            remote.select_thread(1)
            assert remote.get(remote.regs(),'pc')==0
            remote.select_thread(3)
            assert remote.get(remote.regs(),'pc')==0x10020000
            remote.breakpoint(0x1002000c)
            remote.sock.settimeout(10)
            remote.command('vCont;c')
            remote.select_thread(3)
            assert remote.read(0xc1000100,4)==struct.pack('<I',0x87654321)
            remote.select_thread(1)
            assert remote.read(0,24)==code
        finally:
            if remote:remote.sock.close()
            process.terminate();process.wait(timeout=5)
    for option in ('auto-complete=off','blocked-control=0x2002e108'):
        port=free_port()
        negative=[args.qemu,'-M','nikon-z62-dual-lab,native-nor-boot=on','-smp','4',
            '-accel','qtest','-display','none','-serial','none','-monitor','none','-nic','none',
            '-global','nikon-z72-boot-config.'+option,
            '-drive',f'if=pflash,index=0,file={image},format=raw',
            '-qtest',f'tcp:127.0.0.1:{port},server=on,wait=off','-qtest-log',os.devnull]
        process=subprocess.Popen(negative,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        wire=None
        try:
            import time
            deadline=time.monotonic()+10
            while wire is None:
                try:wire=socket.create_connection(('127.0.0.1',port),timeout=2)
                except OSError:
                    assert process.poll() is None and time.monotonic()<deadline
                    time.sleep(.05)
            stream=wire.makefile('rwb')
            def negative_q(command):
                stream.write(command.encode()+b'\n');stream.flush()
                reply=stream.readline().decode().split();assert reply[0]=='OK'
                return int(reply[1],0) if len(reply)>1 else None
            negative_q('writel 0x2002e108 1')
            assert negative_q('readl 0x2002e308')==0,'Failure control was bypassed'
        finally:
            if wire:wire.close()
            process.terminate();process.wait(timeout=5)
print('PASS native NOR alias/reset, SLA isolation/explicit fixture, command-driven handshakes, CFI persistence and preload guards')
