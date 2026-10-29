# SPDX-License-Identifier: GPL-2.0-or-later
"""Portable Windows/WSL entry for the packaged original-handler experiment.

Only uses private NOR copies and QEMU. Never imports WPD or opens USB devices.
The upstream core is unchanged; failed results are retained without write retry.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.nikonunlock_qemu import QemuCamera


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class VisibleCamera(QemuCamera):
    def request(self, opcode, params):
        service = (' / ' + hex(params[0])) if params else ''
        print(f'原厂请求 {opcode:#06x}{service}，等待真实响应……', flush=True)
        start = time.monotonic()
        data, response = super().request(opcode, params)
        print(f'  响应 {response}，数据{len(data)}字节，用时{time.monotonic()-start:.1f}秒', flush=True)
        return data, response


class Session:
    def __init__(self, package, linux_package, run, nor, args):
        self.directory = run
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        relative = lambda path: linux_package + '/' + path.relative_to(package).as_posix()
        self.linux_directory = relative(run)
        baseline = run.parent / (run.name + '-baseline')
        shutil.copytree(package / 'experiments/nikonunlock/baseline', baseline)
        template = (baseline / 'command.template.json').read_text(encoding='utf-8')
        # Replace decoded argument strings, not JSON text, so paths remain valid.
        command = [arg.replace('@PACKAGE@', linux_package).replace('@BASELINE@', relative(baseline))
                   for arg in json.loads(template)]
        save(baseline / 'command.json', command)
        server = ([sys.executable, str(ROOT/'tools/z62_ptp_server.py')] if args.qemu else [
            'wsl.exe', '-d', args.distro, '--exec', 'python3',
            linux_package + '/app/tools/z62_ptp_server.py']) + [
            '--session', relative(run), '--baseline', relative(baseline),
            '--nor', linux_package + '/firmware/NOR_64MiB-original.bin',
            '--nor-store', relative(nor),
            '--card', linux_package + '/experiments/nikonunlock/card-with-keys.raw',
            '--nvme-card', linux_package + '/experiments/nikonunlock/card-with-keys.raw',
            '--qemu', str(args.qemu.resolve()) if args.qemu else linux_package + '/runtime-experimental/qemu-system-arm',
            '--laboratory-pcie-transfer-ready', '--transaction-gap-ms', str(args.transaction_gap_ms)]
        self.log = (run.parent / (run.name + '-server.log')).open('wb')
        self.process = subprocess.Popen(server, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def ready(self):
        print('启动独立实验固件，等待原厂任务就绪（可能需要数分钟）……', flush=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('实验服务器提前退出；查看同目录 server.log')
            failure = self.directory / 'task-install-error.json'
            if failure.is_file():
                raise RuntimeError(failure.read_text(encoding='utf-8'))
            if (self.directory / 'after-task-start.json').is_file():
                if not (self.directory / 'guest-task.json').is_file():
                    raise RuntimeError('原厂实验任务未安装')
                return
            time.sleep(0.5)
        raise TimeoutError('原厂启动等待超时；查看 startup.json 和 server.log')

    def stop(self):
        try:
            if self.process.poll() is None and (self.directory / 'requests').is_dir():
                target = self.directory / 'requests/999-stop.json'
                temporary = target.with_suffix('.tmp')
                save(temporary, {'op': 'stop'})
                temporary.replace(target)
                # A native commit already in progress must finish before stop.
                self.process.wait(timeout=610)
        finally:
            self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=ROOT.parent)
    parser.add_argument('--distro', default='Ubuntu-22.04')
    parser.add_argument('--qemu', type=Path, help='Native Windows executable; bypass WSL')
    parser.add_argument('--transaction-gap-ms', type=int, default=20, help='Explicit guest task scheduling interval')
    parser.add_argument('--nor-file', type=Path, help='Input copied before verify; never written in place')
    parser.add_argument('action', choices=('unlock', 'verify-saved', 'verify'))
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('从Windows Python运行此入口；后台固件自动在WSL运行')
    if sys.version_info < (3, 11):
        parser.error('上游二代核心要求Windows Python 3.11或更新版本')
    if (args.action == 'verify') != (args.nor_file is not None):
        parser.error('verify必须提供--nor-file；其他动作不使用该选项')
    package = args.package.resolve()
    if args.qemu:
        linux_package = package.as_posix()
    else:
        linux_package = subprocess.check_output([
            'wsl.exe', '-d', args.distro, '--exec', 'wslpath', '-a', str(package).replace('\\', '/')
        ], text=True, encoding='utf-8').strip()
        if not linux_package.startswith('/'):
            raise RuntimeError('无法得到WSL中的包目录')
    experiment = package / 'experiments/nikonunlock'
    source = (package / 'firmware/NOR_64MiB-original.bin' if args.action == 'unlock' else
              experiment / 'committed-nor.raw' if args.action == 'verify-saved' else args.nor_file.resolve())
    if source.stat().st_size != 0x4000000:
        raise ValueError('实验NOR必须为64MiB')
    run = package / 'runs-unlock' / (datetime.now().strftime('%Y%m%d_%H%M%S_') +
                                   args.action + '_' + uuid.uuid4().hex[:6])
    run.mkdir(parents=True, exist_ok=False)
    nor = run / 'private-nor.raw'
    shutil.copyfile(source, nor)
    repository = package / 'upstream/NikonUnlock'
    spec = importlib.util.spec_from_file_location('nikonunlock_second_generation', repository / 'unlock_hdmi_raw_ii.py')
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    report = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'action': args.action,
        'input_nor': str(source), 'private_nor': str(nor),
        'layer': 'original ROM callbacks; USB/session observers bypassed',
        'laboratory_pcie_transfer_ready': True, 'transaction_gap_ms': args.transaction_gap_ms,
        'physical_camera_accessed': False, 'write_retry': False, 'stages': []}
    print('本次输出：' + str(run), flush=True)
    print('实验含明确PCIe状态假设；只操作QEMU私有镜像，不访问实机。', flush=True)

    def stage(action, number):
        session = Session(package, linux_package, run / ('session' + str(number)), nor, args)
        record = {'action': action, 'session': str(session.directory)}
        report['stages'].append(record)
        save(run / 'result.json', report)
        try:
            session.ready()
            result = upstream.run(VisibleCamera(session.directory), action, run / action,
                                  repository / 'keys/z6ii_z7ii')
            record['upstream_result'] = result
            record['runtime'] = json.loads((session.directory / 'runtime.json').read_text(encoding='utf-8'))
            return result
        except Exception as error:
            record['error'] = str(error)
            saved = run / action / 'result.json'
            if saved.is_file():
                record['upstream_result'] = json.loads(saved.read_text(encoding='utf-8'))
            raise
        finally:
            try:
                session.stop()
                record['server_stopped'] = session.process.poll() is not None
            finally:
                save(run / 'result.json', report)

    try:
        if args.action == 'unlock':
            stage('unlock', 1)
            print('完整unlock成功，进程已关闭；同一NOR将交给新进程只读verify一次。', flush=True)
        stage('verify', 2 if args.action == 'unlock' else 1)
        if args.action == 'unlock':
            old, new = (value['runtime'] for value in report['stages'])
            if old['qemu_pid'] == new['qemu_pid'] or old['nor_store'] != new['nor_store']:
                raise RuntimeError('新进程/同后端证据不满足预期')
            report['emulator_fresh_process_persistence_verified'] = True
        report['success'] = True
        print('本次流程成功。真实USB、实机RAW外录与完整NOR bootloader仍未验证。', flush=True)
        return 0
    except Exception as error:
        report.update(success=False, error=str(error))
        print('本次流程未全部完成：' + str(error), flush=True)
        if report['stages'] and report['stages'][0].get('upstream_result', {}).get('commit_acknowledged'):
            print('unlock已保存成功；失败发生在随后复核，不能据此判定授权丢失。', flush=True)
        print('保留本次日志；2013为已知间歇I/O问题。没有自动重试写入。', flush=True)
        return 1
    finally:
        save(run / 'result.json', report)
        print('结果文件：' + str(run / 'result.json'), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
