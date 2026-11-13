"""Reassemble the B1-v2 probe pages carried by the four PICCON96..99 saves.

Each save writes a 248-byte file through the camera's card write API; the v2 patch
replaces that buffer with one chunk:

    +0x00 u32 magic   'Z6PD' (0x4450365A)
    +0x04 u8  chunk   0..3
    +0x05 u8  total   4
    +0x06 u16 len     232 (chunks 0..2) or 168 (chunk 3)
    +0x08 u32 offset  chunk * 232
    +0x0C u16 sum     16-bit sum of the payload words
    +0x0E u16 version 1
    +0x10 ..          payload (STATUS+RING slice)

    python np3_chunk_decode.py PICCON96.NP3 PICCON97.NP3 PICCON98.NP3 PICCON99.NP3
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from np3_decode import SITE_NAMES, ST_FIELDS

CHUNK_MAGIC = 0x4450365A
CHUNK_LEN = 864
CHUNK_DATA = 232
CHUNK_TOTAL = 4
STATUS_BYTES = 96
RING_BYTES = 768
NSLOT = 48


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    state = bytearray(CHUNK_LEN)
    seen = {}
    bad = False
    for f in sys.argv[1:]:
        raw = Path(f).read_bytes()
        if len(raw) < 16:
            print(f'{f}: too short ({len(raw)} B)')
            continue
        magic, = struct.unpack_from('<I', raw, 0)
        ch, total = raw[4], raw[5]
        ln, = struct.unpack_from('<H', raw, 6)
        off, = struct.unpack_from('<I', raw, 8)
        s16, ver = struct.unpack_from('<HH', raw, 0xc)
        if magic != CHUNK_MAGIC:
            print(f'{Path(f).name}: not a Z6PD chunk (magic {magic:#010x})')
            continue
        data = raw[16:16 + ln]
        want = sum(int.from_bytes(data[i:i + 2], 'little')
                   for i in range(0, len(data) - 1, 2)) & 0xFFFF
        ok = (ver == 1 and total == CHUNK_TOTAL and ch < total
              and off == ch * CHUNK_DATA and len(data) == ln and s16 == want
              and off + ln <= CHUNK_LEN)
        print(f'{Path(f).name}: chunk {ch}/{total} off {off} len {ln} '
              f'sum {s16:#06x} {"ok" if ok else "BAD"}')
        bad |= not ok
        if ok:
            state[off:off + ln] = data
            seen[ch] = off
    missing = [i for i in range(CHUNK_TOTAL) if i not in seen]
    if missing:
        print(f'\nmissing chunks: {missing} (save to PICCON9{missing[0]}..99 and copy them all)')
        return 1
    if bad:
        return 1
    st = state[:STATUS_BYTES]
    ring = state[STATUS_BYTES:STATUS_BYTES + RING_BYTES]
    print(f'\nassembled {CHUNK_LEN} B from chunks {sorted(seen)}')
    print('STATUS')
    vals = struct.unpack_from('<%dI' % len(ST_FIELDS), st, 0)
    for (name, _), v in zip(ST_FIELDS, vals):
        print(f'  {name:14s} {v:#010x} ({v})')
    print('RING')
    shown = 0
    for i in range(NSLOT):
        slot, a0, a1, lr = struct.unpack_from('<4I', ring, i * 16)
        if slot == 0 and a0 == 0 and a1 == 0 and lr == 0:
            continue
        name = SITE_NAMES[slot] if slot < len(SITE_NAMES) else '?'
        print(f'  [{i:02d}] slot={slot:2d} {name:22s} arg0={a0:#010x} '
              f'arg1={a1:#010x} lr={lr:#010x}')
        shown += 1
    if not shown:
        print('  (empty)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
