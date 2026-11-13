"""Repack the Z6 II image with the pre-shoot patch and re-sign it with K8.

Reads the patch list that hookgen.py writes (patch_spec.txt), calls repack_firmware.py once,
and refuses to continue if the output does not decrypt back to the module we meant to write.

    python work/preshoot_build/build_pkg.py [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import decrypt_firmware as fw                  # noqa: E402
import hookgen as H                            # noqa: E402

STOCK = Path(r'C:\Users\BI\Downloads\Z6_2_0170.bin')
DEFAULT_OUT = HERE / 'Z6_2_0170_preshoot_probe.bin'
EXPECT_LEN = 49_002_888

K8 = bytes.fromhex(os.environ.get('Z6II_K8', ''))
if len(K8) != 8:
    raise SystemExit('set Z6II_K8 to the 8-byte signing key (never commit it)')



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--firmware', default=str(STOCK))
    args = ap.parse_args()
    out = Path(args.out)
    if out.resolve() == Path(args.firmware).resolve():
        raise SystemExit('refusing to overwrite the stock image')

    writes, rep = H.build()
    spec = HERE / 'patch_spec.txt'
    spec.write_text('\n'.join(H.specs(writes)) + '\n', encoding='utf-8')
    print('\n'.join(rep[:4]))
    print(f'... {len(writes)} writes, spec in {spec}')

    cmd = [sys.executable, str(ROOT / 'repack_firmware.py'),
           '--key', str(ROOT / 'key.json'), '--sig', str(ROOT / 'k8.json'),
           '--firmware', args.firmware, '--out', str(out), '--force']
    for s in H.specs(writes):
        cmd += ['--patch-module', s]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout[-800:].rstrip())
    if r.returncode:
        print(r.stderr[-800:])
        raise SystemExit(f'repack failed with {r.returncode}')

    raw = out.read_bytes()
    if len(raw) != EXPECT_LEN:
        raise SystemExit(f'{out} is {len(raw)} B, expected {EXPECT_LEN}')
    key = fw.load_key(str(ROOT / 'key.json'))
    plain = fw.decrypt(raw, *key)
    body = plain[fw.HDR:]
    if hashlib.sha1(body + K8).digest() != plain[:20]:
        raise SystemExit('header digest does not verify with K8')
    mods = fw.validate_firmware(memoryview(body))
    name, off, ln = next(m for m in mods if m[0].startswith('eg1850_mas'))
    mas = bytes(body[off:off + ln - 2])
    if mas != H.apply(writes):
        raise SystemExit('payload in the container is not what hookgen meant to write')
    pkg = struct.unpack_from('>H', body, len(body) - fw.TRAILER)[0]
    print(f'verified    {out.name}: {len(raw)} B, digest {plain[:20].hex()}, '
          f'mas {ln - 2} B, package CRC {pkg:04x}, payload byte-exact')
    return 0


if __name__ == '__main__':
    sys.exit(main())
