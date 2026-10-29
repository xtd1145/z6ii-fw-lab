"""Owned native-MAS-NOR session with read-only traces and physical key requests."""
import os
import argparse,json,re,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.z62_probe import connect,free_port,initialize_inputs,monitor,qtest,sample
from tools.z62_ptp_lab import stopped_thread

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',type=Path,required=True)
    p.add_argument('--nor',type=Path,required=True)
    p.add_argument('--card',type=Path,required=True)
    p.add_argument('--baseline',type=Path,default=ROOT/'.local/nor-native-boot-20261028/session24-worker-abi')
    p.add_argument('--qemu',type=Path)
    p.add_argument('--lifetime',type=int,default=3600)
    p.add_argument('--initial-boot-status',type=lambda x:int(x,0),default=0x200,
                   help='Explicit original branch selector: 200h full init, 0 nonempty MAIN handoff')
    p.add_argument('--watch',action='append',type=lambda x:int(x,0),default=[])
    p.add_argument('--trace-payload',action='store_true',help='Add read-only boundaries after the original file read identifies its allocation')
    p.add_argument('--capture-fixtures',action='store_true',help='Reuse the explicitly experimental R3 sensor completions and two capture boundaries')
    p.add_argument('--diagnostic-preview',action='store_true',help='Label an explicitly selected guest auto-preview diagnostic payload')
    p.add_argument('--payload-watch',action='append',type=lambda x:int(x,0),default=[])
    p.add_argument('--audio-dma',action='store_true',help='Opt into the bounded DMA sink; capture actual samples as audio-dma-capture.wav')
    p.add_argument('--capseq-trace',action='store_true',help='Passive trace of original CAPSEQ request GPIO and mask writes')
    p.add_argument('--capseq-delay-ns',type=int,help='Opt-in upstream-derived actuator; explicitly chosen unmeasured delay')
    p.add_argument('--isp-input-cycle',action='store_true',help='Opt-in TG mask1 input-cycle notification; no image pixels or DMA')
    args=p.parse_args()
    if args.isp_input_cycle and not args.capture_fixtures:
        p.error('--isp-input-cycle requires the explicitly labelled --capture-fixtures TG source')
    directory=args.session.resolve();directory.mkdir(parents=True,exist_ok=False)
    (directory/'requests').mkdir()
    command=json.loads((args.baseline/'command.json').read_text())
    command=[os.devnull if v == "/dev/null" else v for v in command]
    if args.qemu:command[0]=str(args.qemu.resolve())
    for name in ('resident.bin','primary.bin','auxiliary.bin'):
        shutil.copy2(args.baseline/name,directory/name)
        command=[v.replace(str(args.baseline/name),str(directory/name)) for v in command]
    gdb_port,wire_port=free_port(),free_port()
    command[command.index('-gdb')+1]=f'tcp:127.0.0.1:{gdb_port}'
    command[command.index('-qtest')+1]=f'tcp:127.0.0.1:{wire_port},server=on,wait=off'
    with tempfile.TemporaryDirectory(prefix='z62-shutter-') as temp:
        backend=Path(temp)/'private-nor.raw';shutil.copyfile(args.nor,backend)
        for i,value in enumerate(command):
            if value.startswith('if=pflash,'):command[i]=f'if=pflash,index=0,file={backend},format=raw'
        if args.audio_dma:
            command[command.index('-M')+1]+=',laboratory-audio-dma=on'
            command+=['-global',f'nikon-z62-audio-dma.capture-file={directory}/audio-dma-capture.wav']
        if args.capseq_trace:
            command+=['-global','nikon-z72-boot-config.laboratory-capseq-trace=on']
        if args.capseq_delay_ns is not None:
            command+=['-global','nikon-z72-boot-config.laboratory-capseq-actuator=on',
                      '-global',f'nikon-z72-boot-config.capseq-delay-ns={args.capseq_delay_ns}']
        if args.isp_input_cycle:
            command+=['-global','nikon-z72-boot-config.laboratory-isp-input-cycle=on']
        command += ['-global','nikon-z72-boot-config.master-nvme-host=on',
                    '-global','nikon-z72-boot-config.laboratory-pcie-transfer-ready=on',
                    '-drive',f'if=none,id=shutter-card,file={args.card.resolve()},format=raw,snapshot=on',
                    '-device','nvme,drive=shutter-card,serial=Z62SHUTTER0001,bus=dw-pcie,max_ioqpairs=2,msix_qsize=3']
        if args.capture_fixtures:
            for prop in ('nikon-z72-boot-config.frame-start=on',
                         'nikon-z72-input-config.laboratory-metadata-completion=on',
                         'nikon-z72-boot-config.input-command-completion=on',
                         'nikon-z62-sensor-config.laboratory-mode-completion=on',
                         'nikon-z62-sensor-config.laboratory-frame-completion=on',
                         'nikon-z62-sensor-config.laboratory-init-completion=on',
                         'nikon-z62-sensor-config.init-delay-ns=250000000'):
                command+=['-global',prop]
        if os.environ.get('Z62_NVME_TRACE') == '1':
            command += ['-trace','enable=pci_nvme_*','-D',str(directory/'nvme.trace')]
        (directory/'command.json').write_text(json.dumps(command,indent=2))
        process=remote=wire=None;events=[];done=set();watches=set(args.watch);payload_base=None;capture_events=[]
        if args.trace_payload:watches.add(0x1002e35c)
        traps={0x68,0x6c,0x70,0x74,0x78,0x10100004,0x1010000c,0x10100010}
        def ticks():
            remote.select_thread(1)
            return int.from_bytes(remote.read(0x04266008,8),'little')/48000000
        def state(reply):
            nonlocal payload_base
            thread=stopped_thread(reply);remote.select_thread(thread);regs=remote.regs()
            names=('pc','lr','sp','cpsr',*[f'r{i}' for i in range(13)])
            event={'thread':thread,'registers':{n:hex(remote.get(regs,n)) for n in names}}
            if thread<=4:
                event['faults']={n:hex(remote.read_register(n)) for n in ('DFSR_S','DFAR_S','IFSR_S','IFAR_S')}
                for name,size in (('sp',128),('r0',96),('r1',128),('r4',128),('r8',128)):
                    address=remote.get(regs,name)
                    if address:
                        try:event[name+'_memory']=remote.read(address,size).hex()
                        except Exception as error:event[name+'_error']=str(error)
            if args.trace_payload and thread in (1,2) and remote.get(regs,'pc')==0x1002e35c:
                payload_base=remote.get(regs,'r8');length=remote.get(regs,'r9')
                data=remote.read(payload_base,length)
                (directory/'loaded-shcore-payload.bin').write_bytes(data)
                (directory/'payload-runtime.json').write_text(json.dumps({'base':hex(payload_base),'size':length,
                    'allocation_source':'R8 at original file-read completion 1002E35C','guest_seconds':ticks()},indent=2))
                remote.select_thread(thread)
                for offset in (0xea0,0xf20,0xf84,0x10bc,0x1340,0x14c0,0x1640,0x1910,0x1938,0x1c4c,0x2188,0x2424,*args.payload_watch):
                    remote.breakpoint(payload_base+offset);watches.add(payload_base+offset)
            pc=remote.get(regs,'pc')
            if thread in (1,2) and pc in (0x10260c8e,0x10370692):
                try:
                    if pc==0x10260c8e:
                        descriptor=remote.read(remote.get(regs,'r0'),24)
                        address=int.from_bytes(descriptor[:4],'little');length=int.from_bytes(descriptor[4:8],'little')
                        event['pcm_descriptor']=descriptor.hex();label='native-submit'
                    else:
                        address=remote.get(regs,'r1');length=remote.get(regs,'r2');label='dma-submit'
                    if not 0<length<=0x200000:raise ValueError('PCM capture length outside bounded observation')
                    path=directory/f'{label}-{len(events):03}.pcm';path.write_bytes(remote.read(address,length))
                    event['pcm_capture']={'file':str(path),'address':hex(address),'bytes':length}
                except Exception as error:event['pcm_capture_error']=str(error)
            events.append(event);(directory/'events.json').write_text(json.dumps(events,indent=2))
            print('STOP',thread,event['registers']['pc'],flush=True)
            return event
        def run(guest,host=120,trace=False):
            start=ticks();deadline=time.monotonic()+host;stops=[];reason='guest interval'
            while time.monotonic()<deadline and ticks()-start<guest:
                (directory/'run-progress.json').write_text(json.dumps({'guest_seconds':ticks(),
                    'host_seconds_remaining':max(0,deadline-time.monotonic()),'stops':len(stops)},indent=2))
                remote.sock.settimeout(min(2,max(.1,deadline-time.monotonic())))
                try:reply=remote.command('vCont;c')
                except TimeoutError:
                    remote.sock.settimeout(15);remote.interrupt()
                    if args.capture_fixtures and len(capture_events)<2:
                        remote.select_thread(1)
                        def word(a):return int.from_bytes(remote.read(a,4),'little')
                        now=ticks();pending=word(0xc269fa1c)
                        eligible=(word(0xc269fa0c)&4 and word(0xc1888084)>>24==1 and
                                  word(0x20068260)&0x80 and word(0xc1868490)==4)
                        if not capture_events:eligible=eligible and not pending and word(0x20068008)&0x10 and word(0x2006a580)
                        else:eligible=eligible and pending==1 and now-capture_events[-1]['guest_seconds']>=.15
                        if eligible:
                            for level in (1,0):qtest(wire,'set_irq_in /machine/unattached/device[3] input-event 103 '+str(level))
                            capture_events.append({'guest_seconds':now,'pending':pending})
                            (directory/'capture-events.json').write_text(json.dumps(capture_events,indent=2))
                            print('CAPTURE_BOUNDARY',len(capture_events),flush=True)
                    continue
                remote.sock.settimeout(15);event=state(reply);stops.append(event)
                pc=int(event['registers']['pc'],16)
                if pc in traps:reason='native exception';break
                if not trace:reason='breakpoint';break
                if event['thread'] in (1,2):remote.breakpoint(pc,False);watches.discard(pc)
                else:remote.command(f"vCont;s:{event['thread']:x}")
            remote.sock.settimeout(15)
            return {'start_guest_seconds':start,'guest_seconds':ticks(),'reason':reason,'stops':stops}
        def observe(label):
            target=directory/label;target.mkdir()
            report=sample(remote,target);report['guest_seconds']=ticks()
            monitor(remote,'screendump '+json.dumps(str(target/'lcd.ppm'),ensure_ascii=False))
            (target/'application.json').write_text(json.dumps(report,indent=2))
            return {'guest_seconds':report['guest_seconds'],'app_index':report['foreground']['current_index']}
        with (directory/'qemu.log').open('wb') as log:
            try:
                process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=log)
                remote=connect(process,gdb_port,directory/'qemu.log')
                wire=socket.create_connection(('127.0.0.1',wire_port),timeout=10)
                remote.select_thread(1);remote.write(0x0428201c,args.initial_boot_status.to_bytes(4,'little'))
                initialize_inputs(remote,wire,True)
                for address in traps|watches:remote.breakpoint(address)
                (directory/'runtime.json').write_text(json.dumps({'qemu_pid':process.pid,
                    'scope':'native MAS NOR boot, explicit SLA preload and PCIe ready hypothesis',
                    'host_injected_loader_or_playback':False,'physical_camera_accessed':False,
                    'experimental_sensor_fixtures':args.capture_fixtures,
                    'guest_auto_preview_diagnostic':args.diagnostic_preview,
                    'laboratory_audio_dma':args.audio_dma,
                    'initial_boot_status_fixture':hex(args.initial_boot_status),
                    'initial_observation_breakpoints':[hex(a) for a in sorted(watches)],
                    'trace_payload':args.trace_payload,
                    'nvme_trace':os.environ.get('Z62_NVME_TRACE')=='1',
                    'upstream_input_experiment':{'commit':'1a79b9dbdc7bef89f942baf0335b80cb6ba47e71',
                        'capseq_trace':args.capseq_trace,'capseq_delay_ns':args.capseq_delay_ns,
                        'isp_input_cycle':args.isp_input_cycle},
                    'extra_payload_observation_offsets':[hex(a) for a in args.payload_watch]},indent=2))
                print('READY',flush=True)
                deadline=time.monotonic()+args.lifetime
                while time.monotonic()<deadline:
                    requests=sorted(x for x in (directory/'requests').glob('*.json') if x.name not in done)
                    if not requests:time.sleep(.1);continue
                    stop=False
                    for path in requests:
                        job=json.loads(path.read_text());result={}
                        try:
                            op=job['op']
                            if op=='run':result=run(job['guest'],job.get('host',120),job.get('trace',False))
                            elif op=='observe':result=observe(path.stem)
                            elif op=='key':
                                qtest(wire,f"set_irq_in /machine/peripheral/keys pins {int(job['pin'])} {int(job['level'])}")
                                result={'sent':job}
                            elif op=='shutter':
                                listing=monitor(remote,'qom-list /machine/unattached')
                                banks=re.findall(r'(device\[\d+\]) \(child<nikon-z72-controller-gpio>\)',listing)
                                banks.sort(key=lambda v:int(re.search(r'\d+',v)[0]))
                                pin={'half':14,'full':15}[job['stage']]
                                qtest(wire,f"set_irq_in /machine/unattached/{banks[6]} unnamed-gpio-in {pin} {int(job['level'])}")
                                result={'sent':job,'bank':banks[6],'pin':pin}
                            elif op=='read':
                                remote.select_thread(job.get('thread',1))
                                for address,size in job['blocks'].items():
                                    try:result[address]=remote.read(int(address,0),size).hex()
                                    except Exception as error:result[address]={'error':repr(error)}
                            elif op=='registers':
                                remote.select_thread(job.get('thread',1));regs=remote.regs()
                                result={n:hex(remote.get(regs,n)) for n in ('pc','lr','sp','cpsr',*[f'r{i}' for i in range(13)])}
                            elif op=='breakpoint':
                                for a in job.get('add',[]):remote.breakpoint(int(a,0));watches.add(int(a,0))
                                for a in job.get('remove',[]):remote.breakpoint(int(a,0),False);watches.discard(int(a,0))
                            elif op=='dump':
                                remote.select_thread(job.get('thread',1));name=Path(job['name'])
                                if name.is_absolute() or len(name.parts)!=1:raise ValueError('Expected one output filename')
                                data=remote.read(int(job['address'],0),job['size']);(directory/name).write_bytes(data)
                                result={'file':str(directory/name),'size':len(data)}
                            elif op=='monitor':result={'text':monitor(remote,job['command'])}
                            elif op=='stop':stop=True
                            else:raise ValueError(op)
                        except Exception as error:result={'error':repr(error)}
                        (directory/(path.stem+'-result.json')).write_text(json.dumps(result,indent=2))
                        done.add(path.name);print('DONE',path.stem,flush=True)
                        if stop:break
                    if stop:break
            finally:
                if wire:wire.close()
                if remote:remote.sock.close()
                if process and process.poll() is None:process.terminate();process.wait(timeout=5)
                shutil.copyfile(backend,directory/'private-nor.raw')

if __name__=='__main__':main()
