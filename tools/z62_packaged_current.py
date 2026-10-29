# SPDX-License-Identifier: GPL-2.0-or-later
"""Portable, bounded R3.1 NOR-menu and guest shutter-preview checks (Linux/WSL)."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
QEMU_OVERRIDE = None


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def bitmap(path):
    """Give Windows a viewable copy of a QEMU P6 screenshot without extra packages."""
    raw = path.read_bytes()
    header, pixels = raw.split(b'\n', 3)[:3], raw.split(b'\n', 3)[3]
    if header[0] != b'P6' or header[2] != b'255':
        raise ValueError('Expected QEMU P6 RGB screenshot')
    width, height = map(int, header[1].split())
    if len(pixels) != width * height * 3:
        raise ValueError('Incomplete screenshot')
    stride = (width * 3 + 3) & ~3
    output = bytearray()
    for row in range(height - 1, -1, -1):
        rgb = pixels[row*width*3:(row+1)*width*3]
        bgr = bytearray(len(rgb))
        bgr[0::3], bgr[1::3], bgr[2::3] = rgb[2::3], rgb[1::3], rgb[0::3]
        output.extend(bgr)
        output.extend(bytes(stride - width*3))
    head = struct.pack('<2sIHHI', b'BM', 54+len(output), 0, 0, 54)
    head += struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0, len(output), 0, 0, 0, 0)
    path.with_suffix('.bmp').write_bytes(head+output)


def clear_faults(application):
    return all(not any(core['faults'].values()) for core in application['cores'][:4])


def native_menu(package, run, report):
    session = run / 'session'
    command = [sys.executable, str(ROOT/'tools/z62_nor_boot_probe.py'),
        '--qemu', str(QEMU_OVERRIDE or package/'runtime-experimental/qemu-system-arm'),
        '--nor', str(package/'experiments/native-nor/official-main-nor.raw'),
        '--destination', str(session), '--seconds', '600',
        '--controller', str(package/'firmware/controller.bin'),
        '--slave-runtime-fixture', str(package/'firmware/slave.bin'),
        '--initial-boot-status', '0x200', '--frame-sync', '--continue-after-ptp',
        '--menu', '--stop-on-menu', '--navigation-check', '--fast-local-nor']
    print('从原厂NOR Boot启动并验证菜单和方向键，通常需要数分钟……', flush=True)
    with (run/'server.log').open('wb') as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True)
    result = json.loads((session/'result.json').read_text())
    app = json.loads((session/'application.json').read_text())
    if result['stop_reason'] != 'original APPMENU active with display enabled and settled':
        raise RuntimeError('原厂菜单未达到验收点；查看session/result.json')
    if len(result['navigation']) != 5 or not clear_faults(app):
        raise RuntimeError('菜单导航或原厂异常检查未通过')
    before = (session/'menu-before-navigation/lcd.ppm').read_bytes()
    after = (session/'menu-row-down/lcd.ppm').read_bytes()
    if before == after:
        raise RuntimeError('方向键前后画面未发生变化')
    for path in session.rglob('lcd.ppm'):
        bitmap(path)
    report.update(success=True, native_mas_nor_boot=True, menu_navigation=True,
                  slave_runtime_fixture=True, full_dual_processor_cold_boot=False)


def shutter_preview(package, run, report):
    fixture = package/'experiments/shutter'
    baseline = run/'baseline'
    shutil.copytree(fixture/'baseline', baseline)
    template = json.loads((baseline/'command.template.json').read_text(encoding='utf-8-sig'))
    command = [arg.replace('@PACKAGE@', str(package)).replace('@BASELINE@', str(baseline))
               for arg in template]
    save(baseline/'command.json', command)
    session = run/'session'
    server = [sys.executable, str(ROOT/'tools/z62_shutter_lab.py'),
        '--session', str(session), '--baseline', str(baseline),
        '--nor', str(fixture/'preview-nor.raw'), '--card', str(fixture/'card-preview.raw'),
        '--qemu', str(QEMU_OVERRIDE or package/'runtime-experimental/qemu-system-arm'),
        '--initial-boot-status', '0', '--lifetime', '1800',
        '--audio-dma', '--diagnostic-preview', '--trace-payload']
    for address in (0x1002e35c, 0x1002e384, 0x1002e460, 0x10260a44,
                    0x1026089c, 0x10260c8e, 0x10370692, 0x102ded86,
                    0x1036ff92, 0x102e1632, 0x10260f8c):
        server += ['--watch', hex(address)]
    process = None
    number = 0

    def request(job):
        nonlocal number
        number += 1
        label = f'{number:03}-packaged'
        save(session/'requests'/(label+'.json'), job)
        target = session/(label+'-result.json')
        deadline = time.monotonic()+1000
        while time.monotonic() < deadline:
            if target.is_file():
                value = json.loads(target.read_text())
                if 'error' in value:
                    raise RuntimeError(value['error'])
                return value
            if process.poll() is not None:
                raise RuntimeError('实验进程提前退出；查看server.log')
            time.sleep(.25)
        raise TimeoutError('实验请求等待超时；查看server.log')

    print('运行载荷自己的诊断试听，等待原厂文件读取、播放及完成通知……', flush=True)
    with (run/'server.log').open('wb') as output:
        try:
            process = subprocess.Popen(server, stdout=output, stderr=subprocess.STDOUT)
            deadline = time.monotonic()+60
            while not (session/'runtime.json').is_file():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('实验启动失败；查看server.log')
                time.sleep(.25)
            outcome = request({'op':'run', 'guest':8, 'host':900, 'trace':True})
            if outcome['reason'] == 'native exception':
                raise RuntimeError('原厂异常，保留现场；不视为试听成功')
            counts = [int(request({'op':'monitor', 'command':
                'qom-get /machine/master-audio-dma '+name})['text'].strip())
                for name in ('completed-transfers', 'captured-bytes', 'failed-transfers')]
            shot_number = number+1
            request({'op':'observe'})
            app = json.loads((session/f'{shot_number:03}-packaged/application.json').read_text())
            if counts != [1, 38400, 0] or not clear_faults(app):
                raise RuntimeError('声音完成计数或原厂异常检查未通过：'+str(counts))
            with wave.open(str(session/'audio-dma-capture.wav'), 'rb') as sound:
                geometry = (sound.getnchannels(), sound.getsampwidth(), sound.getframerate(), sound.getnframes())
                pcm = sound.readframes(sound.getnframes())
            if geometry != (2,2,48000,9600) or pcm != (fixture/'expected-guest.pcm').read_bytes():
                raise RuntimeError('实际输出声音与已验证的载荷转换结果不一致')
            events = json.loads((session/'events.json').read_text())
            boundaries = {int(e['registers']['pc'],16) for e in events}
            required = {0x10260c8e,0x10370692,0x102ded86,0x1036ff92,0x102e1632,0x10260f8c}
            if not required <= boundaries:
                raise RuntimeError('原厂提交、中断及完成回调记录不完整')
            shutil.copy2(session/'audio-dma-capture.wav', run/'快门声-本次实际输出.wav')
            for path in session.rglob('lcd.ppm'):
                bitmap(path)
            report.update(success=True, guest_auto_preview_diagnostic=True,
                counts_completed_bytes_failed=counts, pcm_exact_match=True,
                native_completion_callbacks=True, physical_shutter_trigger=False,
                native_ui_preview=False, full_dual_processor_cold_boot=False)
        finally:
            if process and process.poll() is None:
                if (session/'requests').is_dir():
                    save(session/'requests/999-stop.json', {'op':'stop'})
                    process.wait(timeout=1000)
                else:
                    process.wait(timeout=60)
            report['owned_server_stopped'] = process is not None and process.poll() is not None


def main():
    global QEMU_OVERRIDE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--qemu', type=Path, help='Native Windows or Linux emulator override')
    parser.add_argument('action', choices=('nor-menu','shutter-preview'))
    args = parser.parse_args()
    QEMU_OVERRIDE = args.qemu.resolve() if args.qemu else None
    package = args.package.resolve()
    run = package/'runs-current'/(datetime.now().strftime('%Y%m%d_%H%M%S_')+args.action+'_'+uuid.uuid4().hex[:6])
    run.mkdir(parents=True)
    report = {'version':'2026-10-28-r3.1', 'action':args.action, 'success':False,
              'created_utc':datetime.now(timezone.utc).isoformat(), 'physical_camera_accessed':False}
    print('本次输出：'+str(run), flush=True)
    try:
        (native_menu if args.action=='nor-menu' else shutter_preview)(package,run,report)
        print('验证通过。结果和本次输出保存在上述目录。', flush=True)
        return 0
    except Exception as error:
        report['error'] = str(error)
        print('本次验证未通过：'+str(error), flush=True)
        return 1
    finally:
        save(run/'result.json', report)


if __name__ == '__main__':
    raise SystemExit(main())
