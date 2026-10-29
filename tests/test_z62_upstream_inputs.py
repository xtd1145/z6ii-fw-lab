# SPDX-License-Identifier: GPL-2.0-or-later
"""Check opt-in upstream-derived inputs: real IRQ wiring, masked latches and cancellation."""
import os
import argparse
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--qemu',required=True);args=p.parse_args()

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]

for mode in ('off','on','timeout'):
    with tempfile.TemporaryDirectory(prefix='z62-upstream-inputs-') as temporary:
        folder=Path(temporary);port,qp=free_port(),free_port()
        command=[args.qemu,'-M','nikon-z62-dual-lab','-smp','4','-accel','qtest',
            '-display','none','-serial','none','-monitor','none','-nic','none',
            '-qtest',f'tcp:127.0.0.1:{port},server=on,wait=off','-qtest-log',os.devnull,
            '-qmp',f'tcp:127.0.0.1:{qp},server=on,wait=off']
        for value in ('frame-sync=on','frame-start=on','laboratory-capseq-trace=on',
                      'laboratory-capseq-actuator='+('off' if mode=='off' else 'on'),
                      'capseq-delay-ns=5000000','capseq-timeout='+('on' if mode=='timeout' else 'off'),
                      'laboratory-isp-input-cycle='+('on' if mode=='on' else 'off')):
            command+=['-global','nikon-z72-boot-config.'+value]
        with (folder/'qemu.log').open('wb') as log:
            process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=log)
            wire=qmp=None
            try:
                deadline=time.monotonic()+10
                while wire is None:
                    try:wire=socket.create_connection(('127.0.0.1',port),timeout=2)
                    except OSError:
                        if process.poll() is not None:raise RuntimeError((folder/'qemu.log').read_text())
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
                stream=wire.makefile('rwb');qmp=socket.create_connection(('127.0.0.1',qp),timeout=2)
                qs=qmp.makefile('rwb');json.loads(qs.readline())
                def query(cmd,arguments=None):
                    data={'execute':cmd}
                    if arguments:data['arguments']=arguments
                    qs.write((json.dumps(data)+'\n').encode());qs.flush()
                    while True:
                        response=json.loads(qs.readline())
                        if 'return' in response:return response['return']
                        if 'error' in response:raise RuntimeError(response)
                query('qmp_capabilities')
                children=query('qom-list',{'path':'/machine/unattached'})
                configs=['/machine/unattached/'+v['name'] for v in children if 'child<nikon-z72-boot-config>'==v['type']]
                master=next(v for v in configs if query('qom-get',{'path':v,'property':'master-role'}))
                slave=next(v for v in configs if v!=master)
                gics=[v['name'] for v in children if 'child<arm_gic>'==v['type']]
                gics.sort(key=lambda x:int(x.split('[')[1].split(']')[0]))
                levels={}
                def q(cmd):
                    stream.write((cmd+'\n').encode());stream.flush()
                    while True:
                        r=stream.readline().decode().split()
                        if r[0]!='IRQ':break
                        levels[int(r[2])]=r[1]=='raise'
                    assert r[0]=='OK',r
                    return int(r[1],0) if len(r)>1 else None
                def wr(a,v):q(f'writel {a:#x} {v:#x}')
                def rd(a):return q(f'readl {a:#x}')
                def step(ns):q(f'clock_step {ns}')
                def count(name,path=master):return query('qom-get',{'path':path,'property':name})
                q('irq_intercept_in /machine/unattached/'+gics[0])
                wr(0x0426f000,1)
                wr(0x04282018,0x100);assert count('capseq-requests')==0
                wr(0x04282018,0x01000100);assert count('capseq-requests')==1
                step(4000000);assert count('capseq-completions')==0
                step(1000001)
                if mode=='on':
                    assert count('capseq-completions')==1 and rd(0x04282014)&0x80
                    assert rd(0x0426f008)&1 and not levels.get(33,False)
                    wr(0x0426f000,0);assert levels.get(33),'CAPSEQ missed real GIC IRQ65 input'
                    wr(0x0426f010,1);assert not levels.get(33) and not rd(0x0426f008)&1
                    wr(0x0426f008,1);assert not rd(0x0426f008)&1,'Software forged pending'
                    wr(0x04282018,0x01000100);step(6000000);assert count('capseq-completions')==1
                    wr(0x04282018,0x01000000);assert not rd(0x04282014)&0x80
                    wr(0x04282018,0x01000100);step(1000000);wr(0x04282018,0x01000000)
                    step(6000000);assert count('capseq-completions')==1 and count('capseq-canceled')==1
                else:
                    assert count('capseq-completions')==0 and not levels.get(33,False)
                    if mode=='timeout':assert not rd(0x04282014)&0x80
                    wr(0x04282018,0x01000000)
                assert count('capseq-requests',slave)==0
                # Original set/clear pairs, explicit enabled input, no invented mask.
                wr(0xc8446040,0x12345678)
                wr(0x20060004,3);wr(0x20060010,1)
                wr(0x20068000,1);wr(0x20068010,1);wr(0x20068008,1)
                wr(0x20068200,0)
                q('set_irq_in '+master+' frame-sync-enable 0 1');step(33333334)
                assert count('isp-input-completed')==0,'Reset engine admitted input'
                wr(0x20068004,1);step(33333334)
                if mode=='on':
                    assert rd(0x20068000)==0 and rd(0x20068008)&1
                    assert count('isp-input-completed')==1 and rd(0x20068210)&1
                    assert not levels.get(62,False)
                    wr(0x20068200,1);assert levels.get(62),'Input cycle missed real IRQ94 input'
                    wr(0x20068210,1);assert not levels.get(62)
                    step(16666665);before=count('isp-input-completed')
                    wr(0x2006800c,1);step(16666668)
                    assert count('isp-input-completed')==before and count('isp-input-canceled')>=1
                    assert not rd(0x20068008)&1
                else:
                    assert count('isp-input-completed')==0
                assert count('isp-input-completed',slave)==0
                assert rd(0xc8446040)==0x12345678,'Input model wrote fake pixel data'
                print('PASS',mode,'CAPSEQ IRQ65 delay/mask/W1C/cancel; ISP IRQ94 gated cycle; peer and pixel isolation')
            finally:
                if wire:wire.close()
                if qmp:qmp.close()
                if process.poll() is None:process.terminate();process.wait(timeout=5)
