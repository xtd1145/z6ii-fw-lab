"""Check actual DMA bytes, virtual duration, GIC IRQ, acknowledgement and rejected requests."""
import os
import argparse,json,socket,struct,subprocess,tempfile,time,wave
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--qemu',required=True);args=p.parse_args()
with tempfile.TemporaryDirectory(prefix='z62-audio-test-') as temp:
    output=Path(temp)/'capture.wav'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));qmp_port=listener.getsockname()[1]
    command=[args.qemu,'-M','nikon-z62-dual-lab,laboratory-audio-dma=on','-smp','4',
             '-accel','qtest','-display','none','-serial','none','-monitor','none','-nic','none',
             '-global',f'nikon-z62-audio-dma.capture-file={output}',
             '-qmp',f'tcp:127.0.0.1:{qmp_port},server=on,wait=off',
             '-qtest',f'tcp:127.0.0.1:{port},server=on,wait=off','-qtest-log',os.devnull]
    process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    wire=qmp=None
    try:
        deadline=time.monotonic()+10
        while wire is None:
            try:wire=socket.create_connection(('127.0.0.1',port),timeout=2)
            except OSError:
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        stream=wire.makefile('rwb')
        qmp=socket.create_connection(('127.0.0.1',qmp_port),timeout=2)
        qmp_file=qmp.makefile('rwb');json.loads(qmp_file.readline())
        def query(command,arguments=None):
            data={'execute':command}
            if arguments:data['arguments']=arguments
            qmp_file.write((json.dumps(data)+'\n').encode());qmp_file.flush()
            while True:
                response=json.loads(qmp_file.readline())
                if 'return' in response:return response['return']
                if 'error' in response:raise RuntimeError(response)
        query('qmp_capabilities')
        gics=[v['name'] for v in query('qom-list',{'path':'/machine/unattached'}) if 'gic' in v['type']]
        gics.sort(key=lambda name:int(name.split('[')[1].split(']')[0]))
        assert len(gics)==2
        irq_levels={}
        def q(command):
            stream.write((command+'\n').encode());stream.flush()
            while True:
                reply=stream.readline().decode().split()
                if reply[0]!='IRQ':break
                irq_levels[int(reply[2])]=reply[1]=='raise'
            assert reply[0]=='OK',reply
            return int(reply[1],0) if len(reply)>1 else None
        def write(address,value):q(f'writel {address:#x} {value:#x}')
        def read(address):return q(f'readl {address:#x}')
        def put(address,data):q(f'write {address:#x} {len(data):#x} 0x{data.hex()}')
        def start(channel,source,target,bytes=128):
            at=0x2c030010+channel*16
            for offset,value in ((4,0x29f80000),(8,source),(12,target),(0,0x83000000|(bytes//4-1))):
                write(at+offset,value)
        def pcm():
            with wave.open(str(output),'rb') as result:
                assert (result.getnchannels(),result.getsampwidth(),result.getframerate())==(2,2,48000)
                return result.readframes(result.getnframes())
        first=struct.pack('<64h',*range(-32,32));second=struct.pack('<64h',*range(31,-33,-1))
        put(0xc8446040,first);put(0xc8447040,second)
        write(0x2c030000,0x90000000)
        write(0x2c080110,0xe0000000);write(0x2c080210,0xe0000000)
        # Observe the real board connection at the GIC input; qtest bus
        # transactions cannot inspect this GIC's secure pending bank.
        q('irq_intercept_in /machine/unattached/'+gics[0])
        start(0,0xc8446040,0x2c080104)
        q('clock_step 1000')
        assert not output.exists(),'Completion occurred before the bounded playback duration'
        assert read(0x2c030010)&0x80000000
        q('clock_step 1000000')
        assert pcm()==first
        assert read(0x2c030014)&0x10000
        assert irq_levels.get(110),'Channel0 did not reach GIC input110 / IRQ142'
        write(0x2c030014,read(0x2c030014)&~0x1f0000)
        assert not read(0x2c030014)&0x10000
        assert not irq_levels.get(110),'Acknowledged IRQ stayed high'
        start(1,0xc8447040,0x2c080204);q('clock_step 1000000')
        assert pcm()==first+second
        assert irq_levels.get(111),'Channel1 did not reach GIC input111 / IRQ143'
        write(0x2c030024,0x29e00000)
        start(0,0xc8446040,0xc8447040);q('clock_step 1000000')
        assert pcm()==first+second and not read(0x2c030014)&0x10000
        start(0,0x01000000,0x2c080104);q('clock_step 1000000')
        assert pcm()==first+second and not read(0x2c030014)&0x10000
        start(0,0xc8446040,0x2c080104);write(0x2c030010,0);q('clock_step 1000000')
        assert pcm()==first+second,'Canceled transfer still emitted samples'
        write(0x2c030014,0x29f90000)
        assert not read(0x2c030014)&0x10000,'Software forged a completion'
        print('PASS: PCM bytes, timing, GIC IRQ142/143 connection, ack, two ports, invalid targets/sources, cancel and no forged done')
    finally:
        if wire:wire.close()
        if qmp:qmp.close()
        if process.poll() is None:process.terminate();process.wait(timeout=5)
