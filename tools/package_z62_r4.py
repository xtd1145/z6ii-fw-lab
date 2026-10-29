# SPDX-License-Identifier: GPL-2.0-or-later
"""Create the R4 full package by reusing unchanged R3 compressed ZIP payloads."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import struct
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Z62-QEMU-实验归档版-20261026-R4'


def text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8-sig', newline='\n')


def prepare(overlay, base):
    overlay.mkdir(parents=True, exist_ok=False)
    app = overlay / 'app'
    for directory in ('analyze', 'qemu', 'tools', 'checks', 'tests', 'docs',
                      'research', 'web', 'web-z62', 'LICENSES', 'artifacts'):
        shutil.copytree(ROOT / directory, app / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('COPYING', 'NOTICE.md', 'README.md', 'README.zh-CN.md', 'README.en.md',
                 'CONTRIBUTING.md', 'publication-manifest.json'):
        shutil.copy2(ROOT / name, app / name)
    copies = {
        '使用教程': 'z62-release-r4-guide.md',
        '与上一版的区别': 'z62-release-r4-changes.md',
        '为什么有问题与还缺什么': 'z62-release-r4-problems.md'}
    for target, source in copies.items():
        content = (ROOT / 'docs' / source).read_text(encoding='utf-8')
        for extension in ('md', 'txt'):
            text(overlay / (target + '.' + extension), content)
    report = (ROOT / 'docs/nikonunlock-qemu-lab.md').read_text(encoding='utf-8')
    report = report.replace('](nikonunlock-progress.md)', '](实验推进与踩坑记录.md)')
    report = report.replace('](nikonunlock/verification-summary.json)',
                            '](experiments/nikonunlock/verification-summary.json)')
    report = '# 包内记录说明\n\n本文件保存开发实验结论；其中 `.local/nikonunlock-qemu-20261026/` 的证据已归档到本包 `experiments/nikonunlock/evidence/`。原始绝对路径是历史证据，新入口会按解压目录生成运行路径。\n\n' + report
    for extension in ('md', 'txt'):
        text(overlay / ('本轮实验结论.' + extension), report)
    progress = (ROOT / 'docs/nikonunlock-progress.md').read_text(encoding='utf-8')
    progress = progress.replace('](nikonunlock-qemu-lab.md)', '](本轮实验结论.md)')
    progress = progress.replace('](nikonunlock/', '](app/docs/nikonunlock/')
    for extension in ('md', 'txt'):
        text(overlay / ('实验推进与踩坑记录.' + extension), progress)
    text(overlay / '先看这里.txt', '''Z62 QEMU · 2026-10-26 R4 实验归档版

完整解压后使用，建议预留12GiB空间。
原有菜单：启动模拟器.cmd（沿用R3稳定程序）
带卡主屏：启动主屏实验.cmd（仍为绿色背景实验）
本轮结论：本轮实验结论.md
问题与缺失功能：为什么有问题与还缺什么.md
完整教程：使用教程.txt

新增实验：验证随包授权样本.cmd / 启动解锁实验.cmd
需Windows Python 3.11+及WSL2 Ubuntu-22.04；流程可能需8–15分钟。
只操作QEMU私有镜像，不连接实机。没有自动写入重试。
新实验使用明确PCIe状态假设；仍有间歇文件I/O，未验证真实RAW外录。

历史菜单/主屏操作能力没有被本次授权实验变成完整相机。
R3原入口仍显示R3版本；实验结果在runs-unlock目录。
''')
    text(overlay / 'start-lab.ps1', '''param(
    [string]$Distro = 'Ubuntu-22.04',
    [ValidateSet('unlock', 'verify-saved')][string]$Mode = 'verify-saved'
)
$ErrorActionPreference = 'Stop'
try {
    $python = Get-Command python -ErrorAction Stop
    & $python.Source -u (Join-Path $PSScriptRoot 'app/tools/z62_packaged_lab.py') --package $PSScriptRoot --distro $Distro $Mode
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
''')
    for name, action in (('启动解锁实验.cmd', 'unlock'), ('验证随包授权样本.cmd', 'verify-saved')):
        content = '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-lab.ps1" -Mode ' + action + '\npause\n'
        (overlay / name).write_bytes(content.replace('\n', '\r\n').encode('utf-8'))
    experiment = overlay / 'experiments/nikonunlock'
    experiment.mkdir(parents=True)
    lab = ROOT / '.local/nikonunlock-qemu-20261026'
    for source in sorted(lab.iterdir()):
        if source.is_dir() and source.name.startswith('session'):
            shutil.copytree(source, experiment / 'evidence' / source.name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        elif source.is_file() and source.suffix in ('.json', '.md', '.txt', '.py'):
            (experiment / 'evidence').mkdir(exist_ok=True)
            shutil.copy2(source, experiment / 'evidence' / source.name)
    shutil.copy2(ROOT / 'docs/nikonunlock/verification-summary.json', experiment / 'verification-summary.json')
    shutil.copy2(lab / 'private-nor-confirm.raw', experiment / 'committed-nor.raw')
    # The two large card files and original NOR are streamed directly into ZIP.
    baseline = experiment / 'baseline'
    baseline.mkdir()
    original_baseline = ROOT / '.local/z62-native-menu-reproduction'
    for name in ('resident.bin', 'primary.bin', 'auxiliary.bin', 'group17.bin', 'group18.bin', 'group32.bin'):
        shutil.copy2(original_baseline / name, baseline / name)
    command = json.loads((original_baseline / 'command.json').read_text())
    replacements = {
        '/home/xuchu/z62-qemu-build/build/qemu-system-arm': '@PACKAGE@/runtime-experimental/qemu-system-arm',
        str(original_baseline).replace('E:', '/mnt/e').replace('\\', '/'): '@BASELINE@'}
    for role, original in (
        ('controller', '/mnt/e/z62/Z62-GPT-交接包/Z62-GPT-交接包/out/features-20261019/recon/osd-hunt/modules/ex1850_012000_bi.bin'),
        ('master', '/mnt/e/z62/Z62-GPT-交接包/Z62-GPT-交接包/out/eg1850_mas_01700.BIN'),
        ('slave', '/mnt/e/z62/Z62-GPT-交接包/Z62-GPT-交接包/out/eg1850_sla_01700.BIN')):
        replacements[original] = '@PACKAGE@/firmware/' + role + '.bin'
    for old, new in replacements.items():
        command = [arg.replace(old, new) for arg in command]
    if any('/mnt/e/' in value or '/home/' in value for value in command):
        raise ValueError('A development path remains in the portable baseline')
    (baseline / 'command.template.json').write_text(json.dumps(command, indent=2), encoding='utf-8')
    upstream = ROOT.parent / 'NikonUnlock'
    shutil.copytree(upstream, overlay / 'upstream/NikonUnlock',
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', 'backups', '.venv'))
    text(overlay / 'upstream/来源说明.txt', '''NikonUnlock: https://github.com/summershrimp/NikonUnlock
Commit: edf2274143f25f3d159efeffa2bc70ef3002e34e
上游源文件原样保存。实验只导入unlock_hdmi_raw_ii.py的run，不导入WPD。
上游文档的二代未实机验证结论保持原样；本包新增的是模拟器证据。
''')
    text(overlay / 'sources/实验构建说明.md', '''# 实验程序源码与构建

原始QEMU 10.2.4归档在本目录；R4当前覆盖源码在`../app/qemu/`，构建入口`../app/tools/build.py`。该源码工作树包含本轮未提交变更，不能仅靠R3历史commit重建实验版。

在Linux/WSL安装编译环境后，可从app目录运行：

```sh
python3 tools/build.py --archive ../sources/qemu-10.2.4.tar.xz --work-dir /tmp/z62-r4-build --jobs 4
```

构建会执行prepare_source.py，选择DesignWare PCIe/NVMe/CFI02并应用固定窗口适配。原有runtime是R3稳定程序；runtime-experimental是本轮实际测试的NVMe/NOR实验程序，通过同包runtime的加载器和库启动。

独立硬件检查入口为tests/test_nvme_host.py、test_parallel_nor.py；还保留原SD、卡检测、DMA及boot-store检查。它们不等同完整相机功能验收。新的包内启动器另作解压运行检查，见根目录打包验证.json。
''')
    with zipfile.ZipFile(base) as z:
        old_root = z.namelist()[0].split('/')[0]
        for name, new in (('使用教程.md', '历史-R3使用教程.md'),
                          ('与上一版的区别.md', '历史-R3与上一版的区别.md'),
                          ('打包验证.json', '历史-R3打包验证.json')):
            (overlay / new).write_bytes(z.read(old_root + '/' + name))
        metadata = json.loads(z.read(old_root + '/包内容.json').decode('utf-8-sig'))
    metadata['base_source_commit'] = metadata.pop('source_commit', None)
    metadata.update(version='2026-10-26-r4-experimental-archive',
        stable_console_version='2026-10-26-r3', incremental_base=base.name,
        source_snapshot='app/ contains the current working tree, including uncommitted experimental changes',
        experimental_runtime='runtime-experimental/qemu-system-arm',
        experimental_results='experiments/nikonunlock/verification-summary.json',
        original_nor='firmware/NOR_64MiB-original.bin',
        known_limitations='为什么有问题与还缺什么.md',
        no_physical_camera_access=True, full_usb_wpd=False, actual_raw_recording=False,
        explicit_pcie_hypothesis=True, intermittent_key_io_2013=True)
    text(overlay / '包内容.json', json.dumps(metadata, ensure_ascii=False, indent=2))


def strip_zip64(extra):
    output = bytearray()
    while extra:
        tag, length = struct.unpack_from('<HH', extra)
        block, extra = extra[:4+length], extra[4+length:]
        if tag != 1:
            output.extend(block)
    return bytes(output)


def reuse(source, destination, info, name):
    if info.flag_bits & 1:
        raise ValueError('Encrypted base entry is not supported')
    target = copy.copy(info)
    target.filename = name
    target.orig_filename = name
    target.flag_bits &= ~8  # Payload has known sizes; no descriptor is copied.
    target.extra = strip_zip64(target.extra)
    destination.fp.seek(destination.start_dir)
    target.header_offset = destination.fp.tell()
    destination._writecheck(target)
    destination._didModify = True
    destination.fp.write(target.FileHeader(target.file_size > zipfile.ZIP64_LIMIT or
                                           target.compress_size > zipfile.ZIP64_LIMIT))
    source.fp.seek(info.header_offset)
    header = source.fp.read(30)
    filename_length, extra_length = struct.unpack_from('<HH', header, 26)
    source.fp.seek(filename_length + extra_length, 1)
    remaining = info.compress_size
    while remaining:
        data = source.fp.read(min(1024*1024, remaining))
        if not data:
            raise EOFError(info.filename)
        destination.fp.write(data)
        remaining -= len(data)
    destination.filelist.append(target)
    destination.NameToInfo[target.filename] = target
    destination.start_dir = destination.fp.tell()


def archive(base, overlay, output, card, nor):
    replacements = {p.relative_to(overlay).as_posix(): p for p in overlay.rglob('*') if p.is_file()}
    replacements['experiments/nikonunlock/card-with-keys.raw'] = card
    replacements['firmware/NOR_64MiB-original.bin'] = nor
    reused = compressed = replaced = 0
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as destination:
        old_root = source.namelist()[0].split('/')[0]
        for info in source.infolist():
            relative = info.filename.removeprefix(old_root + '/')
            if relative == '增量更新记录.json':
                continue
            path = replacements.pop(relative, None)
            if path is not None:
                crc = 0
                if path.stat().st_size == info.file_size:
                    with path.open('rb') as stream:
                        while data := stream.read(1024*1024):
                            crc = zlib.crc32(data, crc)
                if path.stat().st_size != info.file_size or crc != info.CRC:
                    destination.write(path, NAME + '/' + relative)
                    replaced += 1
                    continue
            reuse(source, destination, info, NAME + '/' + relative)
            reused += 1
            compressed += info.compress_size
        for relative, path in sorted(replacements.items()):
            destination.write(path, NAME + '/' + relative)
        record = {'base': base.name, 'new': output.name, 'reused_entries': reused,
                  'reused_compressed_bytes': compressed, 'replaced_entries': replaced,
                  'added_entries': len(replacements),
                  'method': 'unchanged compressed payloads copied without recompression'}
        destination.writestr(NAME + '/增量更新记录.json', json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--overlay', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--archive-only', action='store_true')
    parser.add_argument('--nor', type=Path)
    args = parser.parse_args()
    if not args.archive_only:
        prepare(args.overlay.resolve(), args.base.resolve())
    if not args.prepare_only:
        if not args.output or not args.nor:
            parser.error('--output and --nor are required for archiving')
        archive(args.base.resolve(), args.overlay.resolve(), args.output.resolve(),
                ROOT / '.local/nikonunlock-qemu-20261026/card-with-keys.raw', args.nor.resolve())


if __name__ == '__main__':
    main()
