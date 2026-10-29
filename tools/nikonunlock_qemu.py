# SPDX-License-Identifier: GPL-2.0-or-later
"""Run NikonUnlock's unchanged second-generation core against the QEMU lab.

Uses a private request-file transport, never Windows WPD or a physical camera.
This is a handler integration test: USB/session observers are bypassed. Consult
its native-transactions.jsonl for the original handler's actual responses.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import time


class QemuCamera:
    def __init__(self, directory, timeout=600):
        self.directory = directory.resolve()
        self.timeout = timeout
        self.sequence = 0
        if not (self.directory / 'guest-task.json').is_file():
            raise ValueError('Expected an instrumented original-firmware QEMU session')

    def request(self, opcode, params):
        self.sequence += 1
        name = f'client-{time.time_ns()}-{self.sequence:04d}'
        target = self.directory / 'requests' / (name + '.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(op='ptp', opcode=hex(opcode), parameters=params)), encoding='utf-8')
        temporary.replace(target)
        result_file = self.directory / (name + '-result.json')
        deadline = time.monotonic() + self.timeout
        while True:
            if result_file.is_file():
                try:
                    value = json.loads(result_file.read_text(encoding='utf-8'))
                    break
                except json.JSONDecodeError:
                    # Older private servers publish by write rather than
                    # atomic rename; a visible file may still be incomplete.
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError('No QEMU native response to ' + hex(opcode))
            time.sleep(0.1)
        if 'error' in value:
            raise RuntimeError(value['error'])
        return bytes.fromhex(value['data']), (value['response'][0], value['response'][1])

    def send(self, opcode, params):
        data, response = self.request(opcode, params)
        if data:
            raise RuntimeError('Unexpected data phase for a send operation')
        return response

    def read(self, opcode, params):
        return self.request(opcode, params)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('action', choices=('status', 'snapshot', 'verify', 'unlock'))
    args = parser.parse_args()
    source = args.repository.resolve() / 'unlock_hdmi_raw_ii.py'
    spec = importlib.util.spec_from_file_location('nikonunlock_second_generation', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    camera = QemuCamera(args.session)
    result = module.run(camera, args.action, args.output.resolve(),
                        args.repository.resolve() / 'keys/z6ii_z7ii')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
