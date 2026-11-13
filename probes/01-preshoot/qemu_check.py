"""QEMU cold-boot regression for a pre-shoot package.

Checks the lab's PASS criteria, then reads the DRAM status block over the HMP monitor and
scans the QEMU log for aborts.  Unlike probes/05-qemu-lab/boot_one.py this extracts the mas
payload next to the scripts instead of into .local/, so a probe run leaves nothing outside
this workflow directory.

Write targets: work/preshoot_build/<name>.mas.bi (extracted payload, a copy) and the QEMU
run directory (QEMU's own log).  No NOR, no partition table, no card image (the card is
attached with snapshot=on, so guest writes never reach .local/sdcard.raw).

    python work/preshoot_build/qemu_check.py work/preshoot_build/Z6_2_0170_preshoot_probe.bin
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(r'D:\z6ii_fw_lab')
sys.path.insert(0, str(ROOT / 'probes' / '05-qemu-lab'))
sys.path.insert(0, str(ROOT / 'z62_webui'))
sys.path.insert(0, str(ROOT))
import console as C                            # noqa: E402
import z6ii_patch as zp                        # noqa: E402

HERE = Path(__file__).resolve().parent
AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                for n, a in ((17, 0x132C0000), (18, 0x132E0000), (32, 0x13E80000)))
DEADLOOP = '101034fc'
MASTER = 38750138
CONTAINER = 49002888
STATUS = 0xC5EFF000
SMAGIC = 0x5350365A
FIELDS = ('magic', 'count', 'write_idx', 'frozen', 'retained', 'triggers', 'armed',
          'magic_inv', 'jpeg_hits', 'jpeg_slot', 'jpeg_addr')


def to_mas(src: Path) -> Path:
    size = src.stat().st_size
    if size == MASTER:
        return src
    if size != CONTAINER:
        raise SystemExit(f'unexpected size {size}: {src}')
    _, plain = zp.open_container(str(src), zp.load_key(str(ROOT / 'key.json')))
    body, mods = zp.parse(plain)
    _, off, length = zp.find(mods, 'eg1850_mas')
    dest = HERE / (src.stem + '.mas.bi')
    dest.write_bytes(bytes(body[off:off + length]))
    print(f'extracted mas {length} B -> {dest}')
    return dest


def parse_xp(text: str) -> list[int]:
    clean = re.sub(r'\[[0-9;]*[A-Za-z]', '', text)
    out = []
    for line in clean.splitlines():
        if ':' not in line:
            continue
        toks = line.split(':', 1)[1].replace('0x', '').split()
        if toks and all(re.fullmatch(r'[0-9a-fA-F]{8}', t) for t in toks):
            out += [int(t, 16) for t in toks]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--tag', default='')
    ap.add_argument('--no-status', action='store_true')
    args = ap.parse_args()
    src = Path(args.path)
    master = to_mas(src)

    m = C.Machine()
    cfg = dict(master=str(master),
               slave=str(ROOT / 'fw_verified' / 'eg1850_sla_01700'),
               controller=str(ROOT / 'fw_verified' / 'ex1850_012000.bi'),
               card=str(ROOT / '.local' / 'sdcard.raw'),
               aux=AUX, profile='z62-dual', main_screen=True, logport=False,
               clear_status=True)
    ok = m.start(cfg)
    if not ok.get('ok'):
        print('START FAIL', ok.get('error'))
        return 2
    t0 = time.time()
    guest = None
    while time.time() - t0 < 240:
        time.sleep(3)
        if not m.running():
            print('EXITED early after %.1fs' % (time.time() - t0))
            return 2
        guest = m.guest_seconds()
        if guest is not None and guest >= 2.6:
            break
    gpu = m.gpu_counters()
    pcs = {c['cpu']: c['pc'] for c in m.cores()}
    shot = m.shot()
    from boot_one import png_nonblack          # noqa: E402  (same lab, same metric)
    px = png_nonblack(shot) if shot else 0
    words = []
    raw_status = ''
    if not args.no_status and m.qmp:
        # QMP's human-monitor-command returns clean text; the interactive HMP socket
        # echoes keystrokes with ANSI escapes.
        resp = m.qmp.send({'execute': 'human-monitor-command',
                           'arguments': {'command-line': 'xp/16wx %#x' % STATUS}})
        raw_status = resp.get('return', '') if isinstance(resp, dict) else ''
        words = parse_xp(raw_status)
        if not words:
            print('raw xp output: %r' % raw_status[:200])
    log_path = Path(m.log_path)
    log = log_path.read_text(errors='replace') if log_path.exists() else ''
    m.stop()

    up = gpu.get('successful-uploads') or 0
    rnd = gpu.get('rendered-commands') or 0
    cpu0 = pcs.get(0)
    boot_ok = ((guest or 0) >= 2.4 and up >= 250 and rnd >= 200 and px > 30
               and cpu0 != DEADLOOP)
    aborts = re.findall(r'Taking exception 4.*|Data Abort.*|DFAR 0x[0-9a-f]+', log)
    status = dict(zip(FIELDS, words)) if words else {}
    magic_ok = status.get('magic') == SMAGIC
    passed = boot_ok and (args.no_status or magic_ok) and not aborts

    print(f'{args.tag or src.name} {"PASS" if passed else "FAIL"} guest={guest} up={up} '
          f'rnd={rnd} px={px} cpu0={cpu0}')
    if status:
        print('STATUS  magic=%#x count=%d armed=%d frozen=%d retained=%d jpeg_hits=%d '
              'jpeg_slot=%#x jpeg_addr=%#x'
              % (status['magic'], status['count'], status['armed'], status['frozen'],
                 status['retained'], status['jpeg_hits'], status['jpeg_slot'],
                 status['jpeg_addr']))
    elif not args.no_status:
        print('STATUS  unreadable (no monitor or empty xp output)')
    print(f'aborts  {len(aborts)}' + (': ' + '; '.join(aborts[:3]) if aborts else ''))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
