#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Assemble the local Z62 interactive release and create a verified ZIP."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.z62_probe import IDENTITIES, nor_parameters


START_PS = r'''param([string]$Distro = 'Ubuntu-22.04', [switch]$NoBrowser, [switch]$MainScreen)
$ErrorActionPreference = 'Stop'
$url = 'http://127.0.0.1:8662'
$logDirectory = Join-Path $PSScriptRoot 'runs'
if ($MainScreen) { $url = 'http://127.0.0.1:8663'; $logDirectory = Join-Path $PSScriptRoot 'runs-main' }
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
function Open-Console {
    if ($NoBrowser) { return }
    $ego = Get-Command ego-browser -ErrorAction SilentlyContinue
    if ($ego) {
        $modulePath = Join-Path $PSScriptRoot 'open-egolite.mjs'
        $moduleUrl = ([System.Uri]$modulePath).AbsoluteUri
        & $ego.Source nodejs -e "globalThis.z62ConsoleUrl='$url'; await import('$moduleUrl')"
        if ($LASTEXITCODE -eq 0) { return }
    }
    Start-Process $url
}
try {
    $status = $null
    try { $status = Invoke-RestMethod ($url + '/status') -TimeoutSec 2 } catch {}
    if ($status -and $status.model -eq 'Z62' -and $status.package_version -eq '2026-10-26-r3' -and $status.package_root -eq ((& wsl.exe -d $Distro -- wslpath -a $PSScriptRoot.Replace([char]92, [char]47) | Out-String).Trim())) {
        Write-Host '模拟器已启动，打开现有页面。'
        Open-Console
        exit 0
    }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw '找不到 WSL，请先准备 WSL2 和 Ubuntu-22.04。' }
    $windowsDirectory = $PSScriptRoot.Replace([char]92, [char]47)
    $linuxDirectory = (& wsl.exe -d $Distro -- wslpath -a $windowsDirectory | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $linuxDirectory.StartsWith('/')) { throw '无法访问 WSL 发行版或解压目录。请查看使用教程。' }
    if ($status -and $status.model -eq 'Z62') {
        Invoke-RestMethod -Method Post -Uri ($url + '/stop') -ContentType application/json -Body '{}' | Out-Null
        $closed = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Milliseconds 250
            try { Invoke-RestMethod ($url + '/status') -TimeoutSec 1 | Out-Null } catch { $closed = $true; break }
        }
        if (-not $closed) { throw '旧Z62服务仍在关闭，请稍后重试。' }
    }
    if ($MainScreen) { & wsl.exe -d $Distro --exec python3 ($linuxDirectory + '/launch-background.py') --main-screen-experiment }
    else { & wsl.exe -d $Distro --exec python3 ($linuxDirectory + '/launch-background.py') }
    if ($LASTEXITCODE -ne 0) { throw '启动失败，请查看 runs/startup-error.log。' }
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $status = Invoke-RestMethod ($url + '/status') -TimeoutSec 2
            if ($status.model -eq 'Z62' -and $status.package_version -eq '2026-10-26-r3') { break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $status -or $status.model -ne 'Z62' -or $status.package_version -ne '2026-10-26-r3') { throw '操作页面启动超时，请查看对应日志目录的startup-error.log。' }
    Write-Host ('操作页面：' + $url)
    if ($MainScreen) { Write-Host '正在启动带卡主屏实验，通常需要4–6分钟；绿色背景不是真实取景。' }
    else { Write-Host '正在启动原厂菜单，通常需要1–2分钟。' }
    Open-Console
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
'''


def assemble(args):
    stage = args.stage.resolve()
    stage.mkdir(parents=True, exist_ok=False)
    app = stage / 'app'
    for directory in ('analyze', 'qemu', 'tools', 'checks', 'tests', 'docs',
                      'research', 'web', 'web-z62', 'LICENSES', 'artifacts'):
        shutil.copytree(ROOT / directory, app / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('COPYING', 'NOTICE.md', 'README.md', 'README.zh-CN.md',
                 'CONTRIBUTING.md', 'publication-manifest.json'):
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(args.runtime, stage / 'runtime')
    cards = stage / 'cards'
    cards.mkdir()
    if args.sd_card.stat().st_size != 4 * 1024 ** 3:
        raise ValueError('Expected the verified 4 GiB FAT32 SDHC image')
    shutil.copyfile(args.sd_card, cards / 'sdcard-fat32-4g.raw')
    inputs = stage / 'firmware'
    inputs.mkdir()
    for role, (size, digest) in IDENTITIES.items():
        source = getattr(args, role)
        if source.stat().st_size != size or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Wrong original Z62 {role} input')
        shutil.copyfile(source, inputs / f'{role}.bin')
    parameters = inputs / 'parameters'
    parameters.mkdir()
    nor_parameters(args.nor, parameters)
    (stage / 'sources').mkdir()
    shutil.copyfile(args.qemu_source, stage / 'sources' / 'qemu-10.2.4.tar.xz')
    preview = stage / '预览'
    preview.mkdir()
    for source, name in (
            (ROOT / '.local/z62-native-menu-reproduction/lcd.png', '回放菜单.png'),
            (ROOT / '.local/z62-native-navigation-reproduction/category-right/lcd.png', '拍摄菜单.png'),
            (ROOT / '.local/sensor-chain-20261025/session24-sdhc-fat32/033-run/lcd.png', '带卡主屏-剩余485张.png')):
        shutil.copyfile(source, preview / name)
    evidence = app / 'docs' / 'main-screen-evidence'
    evidence.mkdir()
    shutil.copyfile(ROOT / '.local/sensor-chain-20261025/session24-sdhc-fat32/033-run/report.json', evidence / 'report.json')
    (stage / 'runs').mkdir()
    (stage / 'start.ps1').write_text(START_PS, encoding='utf-8-sig')
    (stage / 'launch-background.py').write_text('''from pathlib import Path
import subprocess, sys

package = Path(__file__).resolve().parent
experiment = '--main-screen-experiment' in sys.argv[1:]
logs = package / ("runs-main" if experiment else "runs")
logs.mkdir(exist_ok=True)
with (logs / "startup.log").open("wb") as output, (logs / "startup-error.log").open("wb") as errors:
    command = ["bash", str(package / "start-linux.sh")]
    if experiment: command.append('--main-screen-experiment')
    process = subprocess.Popen(command,
        stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
(logs / "launcher.pid").write_text(str(process.pid))
print("Z62 process started:", process.pid)
''', encoding='utf-8', newline='\n')
    (stage / 'start-linux.sh').write_text('''#!/bin/sh
set -eu
package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
port=8662
runs="$package/runs"
if [ "${1:-}" = "--main-screen-experiment" ]; then port=8663; runs="$package/runs-main"; fi
exec python3 "$package/app/tools/z62_console.py" \\
  --qemu "$package/runtime/qemu-system-arm" \\
  --master "$package/firmware/master.bin" --slave "$package/firmware/slave.bin" \\
  --controller "$package/firmware/controller.bin" --parameters "$package/firmware/parameters" \\
  --sd-card "$package/cards/sdcard-fat32-4g.raw" \\
  --runs "$runs" --port "$port" "$@"
''', encoding='utf-8', newline='\n')
    (stage / 'open-egolite.mjs').write_text('''const task = await taskSpace(`Z62 simulator ${Date.now()}`);
await task.page("p1").goto(globalThis.z62ConsoleUrl || "http://127.0.0.1:8662");
await task.finish({keep: ["p1"]});
''', encoding='utf-8')
    batch_files = {
        '启动模拟器.cmd': '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"\nif errorlevel 1 pause\n',
        '启动主屏实验.cmd': '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -MainScreen\nif errorlevel 1 pause\n',
        '打开操作页面.cmd': '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"\nif errorlevel 1 pause\n',
        '停止模拟器.cmd': '@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8662/stop -ContentType application/json -Body \'{}\' | Out-Null; Write-Host \'已发送关闭请求。\' } catch { Write-Host \'模拟器已停止或尚未启动。\' }"\n',
    }
    batch_files['停止主屏实验.cmd'] = batch_files['停止模拟器.cmd'].replace('8662', '8663')
    for name, content in batch_files.items():
        (stage / name).write_bytes(content.replace('\n', '\r\n').encode('utf-8'))
    tutorial = (ROOT / 'docs/z62-release-r3-guide.md').read_text(encoding='utf-8')
    changes = (ROOT / 'docs/z62-release-r3-changes.md').read_text(encoding='utf-8')
    for extension in ('md', 'txt'):
        (stage / ('使用教程.' + extension)).write_text(tutorial, encoding='utf-8-sig')
        (stage / ('与上一版的区别.' + extension)).write_text(changes, encoding='utf-8-sig')
        (stage / ('踩坑记录与排查路线.' + extension)).write_text(
            (ROOT / 'docs/z62-pitfalls-and-investigation.md').read_text(encoding='utf-8'),
            encoding='utf-8-sig')
    (stage / '先看这里.txt').write_text('''Z62 原厂菜单模拟器 · 2026-10-26 R3

先解压全部文件，再双击“启动模拟器.cmd”。
操作页面：http://127.0.0.1:8662
通常等待 1–2 分钟自动打开菜单。

已验证：原厂菜单、移动选项、切换分类。
“启动主屏实验.cmd”：带4 GiB虚拟卡的原厂拍摄主屏，通常等待4–6分钟。
主屏实验页面：http://127.0.0.1:8663
虚拟卡已验证原厂FAT32挂载，主屏可显示剩余485张。
MENU收到后可能延后处理，请松开按键并等待画面绘制。
实验仍回放两次外部完成信号；绿色背景不是真实取景，不支持正常拍照。
解压后需约5 GiB空间；卡写入留在临时覆盖层，停止后不保留。

请阅读同目录“使用教程.txt”。此电脑使用已有 WSL2 / Ubuntu-22.04。
新版变化见“与上一版的区别.txt”。
停止时点击网页“关闭模拟器”，或双击“停止模拟器.cmd”。
''', encoding='utf-8-sig')
    metadata = dict(version='2026-10-26-r3', model='Nikon Z6 II', firmware='C1.70',
        platform='Windows + WSL2 / Linux x86_64', http='http://127.0.0.1:8662',
        source_repository='https://github.com/JKWTCN/z72-qemu',
        source_commit='f5f4ba31b324de5abf6f24d399afff878ffd949a',
        original_inputs={role: dict(size=size, sha256=digest)
                         for role, (size, digest) in IDENTITIES.items()},
        verified=['original-menu-display', 'row-navigation', 'menu-category-switch',
                  'native-fat32-mount', 'remaining-shot-count', 'main-menu-round-trip'],
        main_screen_usable=False, raw_firmware_modified=False,
        main_screen_experiment=dict(port=8663, external_boundary_replay=2,
            optical_live_view=False, known_limitations=['green-background', 'no-photo-capture']),
        sd_card=dict(file='cards/sdcard-fat32-4g.raw', size=4 * 1024 ** 3,
                     filesystem='FAT32', temporary_write_overlay=True),
        included_parameters=[17, 18, 32])
    (stage / '包内容.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Assembled:', stage)


def archive(stage, destination):
    if destination.exists():
        raise FileExistsError(destination)
    count = 0
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and 'runs' not in path.relative_to(stage).parts:
                output.write(path, str(Path(stage.name) / path.relative_to(stage)))
                count += 1
    with zipfile.ZipFile(destination) as result:
        failure = result.testzip()
        if failure:
            raise ValueError('ZIP integrity failure: ' + failure)
    print('Verified ZIP:', destination, 'files:', count, 'bytes:', destination.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--runtime', type=Path)
    for role in IDENTITIES:
        parser.add_argument('--' + role, type=Path)
    parser.add_argument('--nor', type=Path)
    parser.add_argument('--qemu-source', type=Path)
    parser.add_argument('--sd-card', type=Path)
    parser.add_argument('--zip-output', type=Path)
    parser.add_argument('--zip-only', action='store_true')
    args = parser.parse_args()
    if not args.zip_only:
        for name in ('runtime', 'master', 'slave', 'controller', 'nor', 'qemu_source', 'sd_card'):
            if getattr(args, name) is None:
                parser.error('--' + name.replace('_', '-') + ' is required when assembling')
        assemble(args)
    if args.zip_output:
        archive(args.stage.resolve(), args.zip_output.resolve())


if __name__ == '__main__':
    main()
