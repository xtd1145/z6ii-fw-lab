# SPDX-License-Identifier: GPL-2.0-or-later
"""Observe native NOR vectors without preloading MAIN, SLA or parameter files."""
import os
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import connect, free_port, laboratory_devices, initialize_inputs, sample, monitor, qtest
from tools.z62_ptp_lab import stopped_thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--nor', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--watch', type=lambda value: int(value, 0), action='append', default=[])
    parser.add_argument('--until',type=lambda value:int(value,0),help='Stop at this native boundary')
    parser.add_argument('--controller', type=Path, help='Explicit original controller firmware; never an engine preload')
    parser.add_argument('--slave-runtime-fixture',type=Path,
                        help='Diagnostic-only SLA preload; disables any claim of native SLA boot')
    parser.add_argument('--block', action='append', default=[], help='Read-only final address:size block')
    parser.add_argument('--initial-boot-status', type=lambda value:int(value,0),
                        help='Explicit laboratory input at 0428201C; real cold-reset value is unknown')
    parser.add_argument('--task',action='append',default=[],type=int,help='Read original task control block and saved stack')
    parser.add_argument('--task-object',action='append',default=[],type=lambda value:int(value,0),
                        help='Read native task object and follow its kernel task ID')
    parser.add_argument('--gpio',action='append',default=[],type=lambda value:int(value,0),help='Resolve original logical GPIO descriptor')
    parser.add_argument('--continue-after-ptp',action='store_true')
    parser.add_argument('--frame-sync',action='store_true',help='Explicit existing periodic external sync fixture')
    parser.add_argument('--capture-fixtures',action='store_true',
                        help='Explicit R3 experimental sensor/input completions; requires frame-sync')
    parser.add_argument('--capture-boundaries',action='store_true',
                        help='Replay the two bounded R3 external capture events; requires capture-fixtures')
    parser.add_argument('--stop-on-menu',action='store_true',help='Stop after APPMENU is active and drawing is enabled')
    parser.add_argument('--menu-settle-guest-seconds',type=float,default=1.0,
                        help='Allow original drawing to continue after APPMENU becomes visible')
    parser.add_argument('--navigation-check',action='store_true',
                        help='After a settled MENU, press/release DOWN and save original frames')
    parser.add_argument('--menu',action='store_true',help='Press/release physical MENU after native app manager initialization')
    parser.add_argument('--fast-local-nor',action='store_true',
                        help='Use a host-local temporary backing, then preserve it in the result directory')
    args = parser.parse_args()
    if args.capture_fixtures and not (args.frame_sync and args.controller):
        parser.error('--capture-fixtures requires --frame-sync and --controller')
    if args.capture_boundaries and not args.capture_fixtures:
        parser.error('--capture-boundaries requires --capture-fixtures')
    if args.navigation_check and not (args.stop_on_menu and args.menu and args.controller):
        parser.error('--navigation-check requires --menu, --stop-on-menu and --controller')
    if args.nor.stat().st_size != 0x4000000:
        parser.error('Expected the recorded 64 MiB NOR window')
    directory = args.destination.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    temporary_backing=tempfile.TemporaryDirectory(prefix='z62-native-nor-') if args.fast_local_nor else None
    backend = Path(temporary_backing.name)/'private-nor.raw' if temporary_backing else directory/'private-nor.raw'
    shutil.copyfile(args.nor, backend)
    gdb_port = free_port()
    machine = 'nikon-z62-dual-lab,native-nor-boot=on'
    if args.slave_runtime_fixture:
        machine += f',slave-runtime-fixture={args.slave_runtime_fixture.resolve()}'
    if args.controller:
        machine += f',controller-firmware={args.controller.resolve()},controller-link=on,controller-battery-peer=on,controller-flash-data={directory}/resident.bin'
    command = [str(args.qemu.resolve()), '-M', machine,
        '-smp', '4,maxcpus=5' if args.controller else '4', '-display', 'none', '-serial', 'none', '-monitor', 'none', '-nic', 'none',
        '-S', '-gdb', f'tcp:127.0.0.1:{gdb_port}', '-icount', 'shift=0,sleep=off',
        '-drive', f'if=pflash,index=0,file={backend},format=raw']
    wire_port = None
    if args.controller:
        wire_port = free_port()
        command += ['-qtest',f'tcp:127.0.0.1:{wire_port},server=on,wait=off','-qtest-log',os.devnull]
        for device in laboratory_devices(directory): command += ['-device',device]
        for channel,value in ((0,1800),(1,3001),(3,3001)):
            command += ['-global',f'nikon-z72-controller-adc.channel{channel}={value}']
    if args.frame_sync:
        command += ['-global','nikon-z72-boot-config.frame-sync=on']
    if args.capture_fixtures:
        for prop in (
            'nikon-z72-boot-config.frame-start=on',
            'nikon-z72-input-config.laboratory-metadata-completion=on',
            'nikon-z72-boot-config.input-command-completion=on',
            'nikon-z62-sensor-config.laboratory-mode-completion=on',
            'nikon-z62-sensor-config.laboratory-frame-completion=on',
            'nikon-z62-sensor-config.laboratory-init-completion=on',
            'nikon-z62-sensor-config.init-delay-ns=250000000',
        ):
            command += ['-global',prop]
    (directory/'command.json').write_text(json.dumps(command, indent=2))
    observations = []
    process = remote = wire = None
    with (directory/'qemu.log').open('wb') as log:
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
            remote = connect(process, gdb_port, directory/'qemu.log')
            if args.initial_boot_status is not None:
                remote.select_thread(1)
                remote.write(0x0428201c,args.initial_boot_status.to_bytes(4,'little'))
            if args.controller:
                wire = socket.create_connection(('127.0.0.1',wire_port),timeout=10)
                initialize_inputs(remote,wire,args.frame_sync)

            def state(label, thread=1):
                remote.select_thread(thread)
                data = remote.regs()
                result = {'label':label, 'thread':thread,
                    'registers':{n:hex(remote.get(data,n)) for n in
                        ('pc','lr','sp','cpsr',*[f'r{i}' for i in range(13)])},
                    'faults':{n:hex(remote.read_register(n)) for n in
                        ('IFSR_S','IFAR_S','DFSR_S','DFAR_S')}}
                for address, size in ((remote.get(data,'pc'),32),(0x04280000,4),
                                      (0x0428201c,4),(0x04008ff0,16)):
                    try: result[hex(address)]=remote.read(address,size).hex()
                    except Exception as error: result[hex(address)]=str(error)
                return result

            initial = [state('reset', thread) for thread in range(1,5)]
            remote.select_thread(1)
            initial_alias = remote.read(0,64).hex()
            (directory/'initial.json').write_text(json.dumps({'cores':initial,'alias':initial_alias},indent=2))
            assert remote.get(remote.regs(),'pc')==0
            watches = {0xe97c,0xea08,0xea2c,0xeddc,0x10020000,0x1048c38e,*args.watch}
            if args.until is not None:watches.add(args.until)
            traps = {0x68,0x6c,0x70,0x74,0x78,0x10100004,0x1010000c,0x10100010}
            for address in watches|traps: remote.breakpoint(address)
            deadline = time.monotonic()+args.seconds
            stop_reason = 'host observation limit'
            menu_pressed_at=None
            menu_released=False
            menu_visible_at=None
            capture_events=[]
            timeline=[]
            def observe_running():
                remote.select_thread(1)
                def word(address):
                    return int.from_bytes(remote.read(address,4),'little')
                ticks=int.from_bytes(remote.read(0x04266008,8),'little')
                manager=remote.read(0xc1811e50,20)
                item={'ticks':ticks,'guest_seconds':ticks/48000000,
                      'app_initialized':int.from_bytes(manager[:4],'little'),
                      'app_index':manager[16],
                      'display_flags':hex(word(0xc1d195b4)),
                      'scanout_flags':hex(word(0x201465c0)),
                      'capture_flags':hex(word(0xc269fa0c)),
                      'capture_pending':word(0xc269fa1c),
                      'capture_mode':word(0xc1888084)>>24,
                      'capture_armed':hex(word(0x20068260)),
                      'capture_state':word(0xc1868490),
                      'menu_request':hex(word(0xc181dae8))}
                item['sensor']={prop:json.loads(monitor(remote,'qom-get /machine/master-sensor '+prop))
                                for prop in ('init-submissions','init-completions','mode-completions',
                                             'mode2-completions','frame-completions')}
                if args.capture_boundaries and len(capture_events)<2:
                    eligible=(int(item['capture_flags'],16)&4 and item['capture_mode']==1 and
                              int(item['capture_armed'],16)&0x80 and item['capture_state']==4)
                    if not capture_events:
                        eligible=eligible and not item['capture_pending'] and word(0x20068008)&0x10 and word(0x2006a580)
                    else:
                        eligible=eligible and item['capture_pending']==1 and ticks-capture_events[-1]['ticks']>=7200000
                    if eligible:
                        for level in (1,0):
                            qtest(wire,'set_irq_in /machine/unattached/device[3] input-event 103 '+str(level))
                        capture_events.append(dict(item))
                        print('CAPTURE',len(capture_events),'ticks',ticks,flush=True)
                timeline.append(item)
                (directory/'timeline.json').write_text(json.dumps(timeline,indent=2))
                return item
            while time.monotonic()<deadline:
                remote.sock.settimeout(min(2,max(.1,deadline-time.monotonic())))
                try: reply = remote.command('vCont;c')
                except TimeoutError:
                    remote.sock.settimeout(15)
                    remote.interrupt()
                    running=observe_running() if args.controller else None
                    if args.menu and wire:
                        remote.select_thread(1)
                        ticks=int.from_bytes(remote.read(0x04266008,8),'little')
                        manager=remote.read(0xc1811e50,20)
                        # Reuse the stable console's native-input boundary:
                        # APPLV active and at least three guest seconds.
                        if menu_pressed_at is None and int.from_bytes(manager[:4],'little')==1 and manager[16]==3 and ticks>=144000000:
                            qtest(wire,'set_irq_in /machine/peripheral/keys pins 9 0')
                            menu_pressed_at=ticks
                        elif menu_pressed_at is not None and not menu_released and ticks-menu_pressed_at>=7200000:
                            qtest(wire,'set_irq_in /machine/peripheral/keys pins 9 1')
                            menu_released=True
                    if args.stop_on_menu and running and running['app_index']==1 and int(running['display_flags'],16)&1 and int(running['scanout_flags'],16)&1:
                        if menu_visible_at is None:
                            menu_visible_at=running['ticks']
                            print('MENU_DISPLAY_ENABLED',running['guest_seconds'],flush=True)
                        if running['ticks']-menu_visible_at>=args.menu_settle_guest_seconds*48000000:
                            stop_reason='original APPMENU active with display enabled and settled'
                            break
                    continue
                remote.sock.settimeout(15)
                thread = stopped_thread(reply)
                event = state('breakpoint',thread)
                observations.append(event)
                (directory/'observations.json').write_text(json.dumps(observations,indent=2))
                pc = int(event['registers']['pc'],16)
                if pc in args.watch and thread in (1,2):
                    try:event['saved_stack']=remote.read(int(event['registers']['sp'],16),0x80).hex()
                    except Exception as error:event['saved_stack_error']=str(error)
                    pointer=int(event['registers']['r4'],16)
                    if 0xc0000000<=pointer<0xd0000000:
                        try:event['r4_memory']=remote.read(pointer,0x60).hex()
                        except Exception as error:event['r4_memory_error']=str(error)
                    (directory/'observations.json').write_text(json.dumps(observations,indent=2))
                print('STOP',thread,hex(pc),flush=True)
                if pc in traps:
                    stop_reason='native exception boundary'
                    break
                if pc==args.until and thread in (1,2):
                    stop_reason='requested native boundary reached'
                    break
                if pc==0x1048c38e:
                    if not args.continue_after_ptp:
                        stop_reason='original PTP wait loop reached'
                        break
                if thread in (1,2) or pc not in (0x10020000,0x1048c38e,*args.watch):
                    remote.breakpoint(pc,False)
                else:
                    remote.command(f'vCont;s:{thread:x}')
            remote.sock.settimeout(15)
            navigation=[]
            if args.navigation_check and stop_reason=='original APPMENU active with display enabled and settled':
                def navigation_snapshot(label):
                    target=directory/label
                    target.mkdir()
                    snapshot=sample(remote,target)
                    remote.select_thread(1)
                    snapshot['guest_seconds']=int.from_bytes(remote.read(0x04266008,8),'little')/48000000
                    monitor(remote,'screendump '+json.dumps(str(target/'lcd.ppm'),ensure_ascii=False))
                    (target/'application.json').write_text(json.dumps(snapshot,indent=2))
                    navigation.append({'label':label,'guest_seconds':snapshot['guest_seconds'],
                                       'app_index':snapshot['foreground']['current_index']})
                def guest_window(seconds):
                    remote.select_thread(1)
                    end=int.from_bytes(remote.read(0x04266008,8),'little')+int(seconds*48000000)
                    limit=time.monotonic()+120
                    while time.monotonic()<limit:
                        remote.select_thread(1)
                        if int.from_bytes(remote.read(0x04266008,8),'little')>=end:return
                        remote.sock.settimeout(2)
                        try:
                            reply=remote.command('vCont;c')
                        except TimeoutError:
                            remote.sock.settimeout(15)
                            remote.interrupt()
                            continue
                        raise RuntimeError('Unexpected native stop during navigation: '+reply)
                    raise TimeoutError('Navigation guest-time observation limit')
                navigation_snapshot('menu-before-navigation')
                for label,pin in (('menu-row-down',4),('menu-category-left',7),
                                  ('menu-category-down',4),('menu-category-right',6)):
                    for level in (0,1):
                        qtest(wire,f'set_irq_in /machine/peripheral/keys pins {pin} {level}')
                        guest_window(.15)
                    guest_window(.25)
                    navigation_snapshot(label)
            final = [state('final',thread) for thread in range(1,5)]
            remote.select_thread(1)
            blocks={}
            args.block += ['0xc1887cb0:0x100','0xc1868400:0x200','0xc269fa00:0x100','0x20146000:0x600']
            args.block += [f'{address:#x}:4' for address in (0x20065400,0x20065504,0x20065508,0x2006550c,
                           0x20068008,0x20068260,0x20068104,0x20060040,0x20060044,0x20061000,0x200610ec)]
            for block in args.block:
                address,size=(int(value,0) for value in block.split(':'))
                try: blocks[hex(address)]=remote.read(address,size).hex()
                except Exception as error: blocks[hex(address)]={'error':str(error)}
            for address in (0xc2b2a934,0xc2b2a938):
                try:
                    tcb=int.from_bytes(remote.read(address,4),'little')
                    if tcb:
                        data=remote.read(tcb,0x100)
                        blocks[hex(tcb)]=data.hex()
                        stack=int.from_bytes(data[0xbc:0xc0],'little')
                        if stack: blocks[hex(stack)]=remote.read(stack,0x180).hex()
                except Exception as error: blocks[hex(address)]={'error':str(error)}
            tasks={}
            gpios={}
            for logical in args.gpio:
                mapping=int.from_bytes(remote.read(0xc1800008,4),'little')
                index=int.from_bytes(remote.read(mapping+2*logical,2),'little')
                descriptor=int.from_bytes(remote.read(0xc18aedc8+4*index,4),'little')
                data=remote.read(descriptor,16)
                bit=data[4]
                register=int.from_bytes(data[8:12],'little')
                value=int.from_bytes(remote.read(register,4),'little')
                gpios[hex(logical)]={'mapping':hex(mapping),'index':index,'descriptor':hex(descriptor),
                    'raw_descriptor':data.hex(),'register':hex(register),'bit':bit,
                    'register_value':hex(value),'level':(value>>bit)&1}
            task_objects={}
            for address in args.task_object:
                data=remote.read(address,0x40)
                task_objects[hex(address)]=data.hex()
                task_id=int.from_bytes(data[:4],'little')&0xffff
                if task_id: args.task.append(task_id)
            if args.task:
                base=int.from_bytes(remote.read(0xc18ad81c,4),'little')
                count=int.from_bytes(remote.read(0xc18ad820,4),'little')
                for task in args.task:
                    if not base or not 1<=task<=count: continue
                    address=base+(task-1)*0xd8
                    data=remote.read(address,0xd8)
                    stack=int.from_bytes(data[0xbc:0xc0],'little')
                    value={'address':hex(address),'data':data.hex(),'saved_sp':hex(stack)}
                    if stack:
                        try: value['saved_stack']=remote.read(stack,0x180).hex()
                        except Exception as error: value['saved_stack_error']=str(error)
                    tasks[str(task)]=value
            if args.controller:
                try:
                    application=sample(remote,directory)
                    (directory/'application.json').write_text(json.dumps(application,indent=2))
                    monitor(remote,'screendump '+json.dumps(str(directory/'lcd.ppm'),ensure_ascii=False))
                except Exception as error:
                    (directory/'application-error.json').write_text(json.dumps({'error':repr(error)}))
            result={'nor_source':str(args.nor.resolve()),'private_nor':str(backend),
                'runtime_host_local_nor':args.fast_local_nor,
                'durable_nor_copy':str(directory/'private-nor.raw'),
                'mode':'native MAS NOR vectors; diagnostic SLA preload' if args.slave_runtime_fixture else
                       'native NOR vectors; no MAIN/SLA/aux preload; SLA powered off',
                'initial_alias':initial_alias,'initial':initial,'observations':observations,
                'final':final,'stop_reason':stop_reason,
                'controller_firmware':str(args.controller) if args.controller else None,
                'slave_runtime_fixture':str(args.slave_runtime_fixture) if args.slave_runtime_fixture else None,
                'controller_inputs_are_laboratory_fixtures':bool(args.controller),
                'initial_boot_status_fixture':hex(args.initial_boot_status) if args.initial_boot_status is not None else None,
                'periodic_frame_sync_fixture':args.frame_sync,
                'capture_completion_fixtures':args.capture_fixtures,
                'bounded_capture_boundary_fixture':args.capture_boundaries,
                'capture_boundary_events':capture_events,
                'timeline':timeline,
                'menu_pressed_ticks':menu_pressed_at,'menu_released':menu_released,
                'menu_display_enabled_ticks':menu_visible_at,
                'navigation':navigation,
                'final_blocks':blocks,
                'tasks':tasks,
                'gpios':gpios,
                'task_objects':task_objects,
                'physical_reset_mapping_verified':False,'physical_camera_accessed':False}
            (directory/'result.json').write_text(json.dumps(result,indent=2))
            print(json.dumps({'stop_reason':stop_reason,'final_pcs':[value['registers']['pc'] for value in final]}),flush=True)
        finally:
            if wire: wire.close()
            if remote: remote.sock.close()
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            if temporary_backing:
                shutil.copyfile(backend,directory/'private-nor.raw')
                temporary_backing.cleanup()


if __name__=='__main__': main()
