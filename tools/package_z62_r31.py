# SPDX-License-Identifier: GPL-2.0-or-later
"""Build a full R3.1 ZIP, reusing unchanged compressed entries from R4."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_z62_r4 import reuse, text

NAME = 'Z62-QEMU-20261028-R3.1'
VERSION = '2026-10-28-r3.1'


def evidence(source, target):
    omitted = []
    for item in source.rglob('*'):
        if not item.is_file() or '__pycache__' in item.parts:
            continue
        relative = item.relative_to(source)
        if (item.suffix in ('.raw', '.pyc') or item.name in ('resident.bin','primary.bin','auxiliary.bin')
                or item.stat().st_size > 4*1024*1024):
            omitted.append({'path':relative.as_posix(), 'bytes':item.stat().st_size})
            continue
        destination = target/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
    text(target/'重复大文件省略说明.json', json.dumps({
        'scope':'Historical logs and bounded dumps; repeated disk/firmware copies excluded.',
        'reproduction':'Use package launchers and the named shared input images, not historical absolute paths.',
        'omitted':omitted}, ensure_ascii=False, indent=2))


def prepare(base, overlay, binary):
    overlay.mkdir(parents=True, exist_ok=False)
    app = overlay/'app'
    for directory in ('analyze','qemu','tools','checks','tests','docs','research','web','web-z62','LICENSES','artifacts'):
        shutil.copytree(ROOT/directory, app/directory,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('COPYING','NOTICE.md','README.md','README.zh-CN.md','README.en.md','CONTRIBUTING.md','publication-manifest.json'):
        shutil.copy2(ROOT/name, app/name)
    for target, source in (
        ('使用教程','z62-release-r31-guide.md'),('与上一版的区别','z62-release-r31-changes.md'),
        ('项目进展总结','z62-progress-r31.md')):
        content = (ROOT/'docs'/source).read_text(encoding='utf-8')
        for extension in ('md','txt'):
            text(overlay/(target+'.'+extension), content)
    problems = (ROOT/'docs/z62-release-r4-problems.md').read_text(encoding='utf-8')
    problems = '# R3.1问题与缺失功能（2026-10-28）\n\n最新补充见“项目进展总结.md”和“使用教程.md”。MAS原厂NOR Boot菜单、诊断音频发送已经通过；完整双处理器冷启动、正常快门与自定义菜单仍未通过。以下保留26日问题详情，涉及当时尚未完成的项目以28日补充为准。\n\n'+problems
    for extension in ('md','txt'):
        text(overlay/('为什么有问题与还缺什么.'+extension), problems)
    text(overlay/'先看这里.txt', '''Z62 QEMU · R3.1 · 2026-10-28

完整解压，建议预留16GiB空间。Windows使用已有WSL2 / Ubuntu-22.04。
启动模拟器.cmd：可操作原厂菜单，优先使用Egolite。
启动主屏实验.cmd：带卡主屏，绿色背景不是真实取景。
运行原厂NOR引导验证.cmd：从原厂Boot启动MAS，验证菜单/方向键后自动关闭。
运行快门声播放验证.cmd：诊断试听，完成后在runs-current中生成本次实际输出WAV。
验证随包授权样本.cmd / 启动解锁实验.cmd：沿用原解锁实验，需Windows Python3.11+。

声音正常快门触发及自定义菜单仍未通过；完整USB、RAW外录、双处理器冷启动未通过。
所有新实验只运行QEMU私有镜像。教程见“使用教程.txt”，总结见“项目进展总结.md”。
普通菜单保留原R3运行程序；独立实验使用28日最新运行程序，整个包统一命名R3.1。
''')
    text(overlay/'start-current.ps1', '''param(
    [string]$Distro = 'Ubuntu-22.04',
    [ValidateSet('nor-menu','shutter-preview')][string]$Mode = 'nor-menu'
)
$ErrorActionPreference = 'Stop'
try {
    $linuxDirectory = (& wsl.exe -d $Distro --exec wslpath -a $PSScriptRoot.Replace([char]92,[char]47) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $linuxDirectory.StartsWith('/')) { throw '无法访问WSL或解压目录。' }
    & wsl.exe -d $Distro --exec python3 ($linuxDirectory + '/app/tools/z62_packaged_current.py') --package $linuxDirectory $Mode
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
''')
    for name, mode in (('运行原厂NOR引导验证.cmd','nor-menu'),('运行快门声播放验证.cmd','shutter-preview')):
        data = '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-current.ps1" -Mode '+mode+'\npause\n'
        (overlay/name).write_bytes(data.replace('\n','\r\n').encode('utf-8'))
    with zipfile.ZipFile(base) as source:
        old = source.namelist()[0].split('/')[0]+'/'
        start = source.read(old+'start.ps1').decode('utf-8-sig').replace('2026-10-26-r3',VERSION)
        text(overlay/'start.ps1', start)
        for name in ('使用教程.md','与上一版的区别.md','打包验证.json'):
            (overlay/('历史-R4-'+name)).write_bytes(source.read(old+name))
        metadata = json.loads(source.read(old+'包内容.json').decode('utf-8-sig'))
    # The stable binary/libs/card remain the original R3 payloads in the base ZIP.
    target = overlay/'runtime-experimental/qemu-system-arm.bin'
    target.parent.mkdir()
    shutil.copy2(binary, target)
    metadata.update(version=VERSION, display_version='R3.1', naming_date='2026-10-28',
        stable_console_version=VERSION, stable_runtime_origin='R3 verified menu runtime retained',
        experimental_runtime_origin='2026-10-28 build-native-nor, including opt-in audio DMA',
        incremental_base=base.name, current_nor_menu_entry='运行原厂NOR引导验证.cmd',
        current_sound_entry='运行快门声播放验证.cmd',
        diagnostic_audio_playback_verified=True, physical_shutter_audio=False,
        native_custom_audio_menu=False, native_mas_boot_menu=True,
        full_dual_processor_cold_boot=False, source_snapshot='app/ current working tree including uncommitted changes',
        original_firmware_inputs_modified=False, contains_private_patched_nor=True,
        current_sound_results='app/docs/shutter-playback-verification-20261028.json')
    metadata.pop('raw_firmware_modified',None)
    text(overlay/'包内容.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    native = overlay/'experiments/native-nor'
    native.mkdir(parents=True)
    shutil.copy2(ROOT/'.local/nor-native-boot-20261027/official-main-nor.raw',native/'official-main-nor.raw')
    evidence(ROOT/'.local/nor-native-boot-20261027',native/'evidence-27')
    evidence(ROOT/'.local/nor-native-boot-20261028',native/'evidence-28')
    sound = overlay/'experiments/shutter'
    sound.mkdir()
    lab = ROOT/'.local/shutter-playback-20261028'
    shutil.copy2(lab/'session30-preview-dma/private-nor.raw',sound/'preview-nor.raw')
    shutil.copy2(lab/'card-preview-diagnostic.raw',sound/'card-preview.raw')
    shutil.copy2(lab/'session27-address-abi-payload/converted-wave-48k-stereo.pcm',sound/'expected-guest.pcm')
    evidence(lab,sound/'evidence')
    for name,source,notice in (
        ('NOR引导与移植推进','z62-nor-port-progress-20261028.md',
         '本文保留此前逐项因果对照；其中当时剩余四处MAIN目标已在本次声音实验私有副本修正。'),
        ('快门声载荷与播放记录','z62-shutter-playback-lab.md',
         '本文保存最新声音播放成功及正常入口未通过的证据。')):
        content=(ROOT/'docs'/source).read_text(encoding='utf-8')
        content=content.replace('../.local/nor-native-boot-20261028/','experiments/native-nor/evidence-28/')
        content=content.replace('../.local/shutter-playback-20261028/','experiments/shutter/evidence/')
        content=content.replace('../../../快门声-模拟器DMA输出-20261028.wav','声音样例/快门声-已验证DMA输出.wav')
        content=content.replace('(nor-port-progress-20261028.json)','(app/docs/nor-port-progress-20261028.json)')
        content=content.replace('(shutter-playback-verification-20261028.json)','(app/docs/shutter-playback-verification-20261028.json)')
        content='包内说明：'+notice+' 历史路径为原记录，重复大镜像已省略；复现请使用包内启动器。\n\n'+content
        for extension in ('md','txt'):
            text(overlay/(name+'.'+extension),content)
    baseline = sound/'baseline'
    baseline.mkdir()
    original = ROOT/'.local/nor-native-boot-20261028/session24-worker-abi'
    for name in ('resident.bin','primary.bin','auxiliary.bin'):
        shutil.copy2(original/name,baseline/name)
    command = json.loads((original/'command.json').read_text())
    replacements = {
        str(original).replace('E:', '/mnt/e').replace('\\','/'):'@BASELINE@',
        '/mnt/e/z62/Z62-GPT-交接包/Z62-GPT-交接包/out/eg1850_sla_01700.BIN':'@PACKAGE@/firmware/slave.bin',
        '/mnt/e/z62/Z62-GPT-交接包/Z62-GPT-交接包/out/features-20261019/recon/osd-hunt/modules/ex1850_012000_bi.bin':'@PACKAGE@/firmware/controller.bin'}
    command[0] = '@PACKAGE@/runtime-experimental/qemu-system-arm'
    for old,new in replacements.items():
        command = [value.replace(old,new) for value in command]
    command = ['if=pflash,index=0,file=@NOR@,format=raw' if value.startswith('if=pflash,') else value for value in command]
    if any('/mnt/e/' in value or '/home/' in value or '/tmp/' in value for value in command):
        raise ValueError('Development path left in portable NOR baseline')
    text(baseline/'command.template.json',json.dumps(command,indent=2))
    previews = overlay/'预览'
    previews.mkdir()
    for src,dest in (
        (ROOT/'.local/nor-native-boot-20261028/session20-native-long-control/lcd.png','原厂NOR引导-设定菜单.png'),
        (ROOT/'.local/shutter-playback-20261028/session31-native-ui-preview/082-beep-switch/lcd.png','原生UI尝试-仍是原厂蜂鸣音页.png')):
        shutil.copy2(src,previews/dest)
    sample = overlay/'声音样例'
    sample.mkdir()
    shutil.copy2(lab/'session30-preview-dma/audio-dma-capture.wav',sample/'快门声-已验证DMA输出.wav')
    text(sample/'说明.txt','此样例为session30的真实DMA输出；运行“运行快门声播放验证.cmd”会在runs-current下生成新的输出供比较。正常快门触发尚未通过。\n')
    text(overlay/'sources/R3.1构建说明.md', '''# R3.1源码与构建

app/是28日当前源代码，包含尚未提交的NOR/音频实验变更。原始QEMU源码归档、版权及运行库沿用包内sources/runtime。
普通菜单程序保留R3版本，runtime-experimental/qemu-system-arm.bin为28日实际编译的最新程序，配套同包加载器及库。

Linux/WSL可从app目录构建：
python3 tools/build.py --archive ../sources/qemu-10.2.4.tar.xz --work-dir /tmp/z62-r31-build --jobs 4

新入口app/tools/z62_packaged_current.py提供有边界的nor-menu/shutter-preview复现；不依赖开发机目录。
历史证据中的重复NOR、卡和大内存副本已省略并逐项登记，共享可运行输入在experiments/native-nor和experiments/shutter。
''')
    print('Prepared R3.1 overlay:',overlay,flush=True)


def archive(base, overlay, output):
    replacements = {path.relative_to(overlay).as_posix():path for path in overlay.rglob('*') if path.is_file() and '__pycache__' not in path.parts}
    record = {'version':VERSION,'base':base.name,'reused_entries':0,'replaced_entries':0,'added_entries':0,'reused_compressed_bytes':0}
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as target:
        old = source.namelist()[0].split('/')[0]+'/'
        for info in source.infolist():
            relative = info.filename.removeprefix(old)
            if relative == '增量更新记录.json':
                continue
            path = replacements.pop(relative,None)
            if path is not None:
                same = False
                if path.stat().st_size == info.file_size:
                    crc = 0
                    with path.open('rb') as stream:
                        while block := stream.read(1024*1024):
                            crc = zlib.crc32(block,crc)
                    same = crc == info.CRC
                if not same:
                    target.write(path, NAME+'/'+relative)
                    record['replaced_entries'] += 1
                    continue
            reuse(source,target,info,NAME+'/'+relative)
            record['reused_entries'] += 1
            record['reused_compressed_bytes'] += info.compress_size
        for relative,path in sorted(replacements.items()):
            target.write(path,NAME+'/'+relative)
            record['added_entries'] += 1
        target.writestr(NAME+'/增量更新记录.json',json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps(record,ensure_ascii=False,indent=2),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--overlay',type=Path,required=True)
    parser.add_argument('--binary',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--archive-only',action='store_true')
    parser.add_argument('--prepare-only',action='store_true')
    args = parser.parse_args()
    if not args.archive_only:
        if not args.binary:
            parser.error('--binary is required to prepare')
        prepare(args.base,args.overlay,args.binary)
    if not args.prepare_only:
        if not args.output:
            parser.error('--output is required to archive')
        archive(args.base,args.overlay,args.output)


if __name__ == '__main__':
    main()
