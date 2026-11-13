"""Decode the Z6PD page(s) that the B1 patch writes into an exported Picture Control.

The NP3 tone-curve TLV (tag 0x0002, len 0x242) carries a 256-word table at payload +0x40.
On the save path the camera converts that table from little to big endian and appends it to
the file, so the table lands at different file offsets depending on how many TLVs precede
it: v1.70's own export is 0x342 B with the table at +0x13C, while the extended 1234 B
variant (extra 0x1F00/0x2000/0x10100 blocks) puts it at +0x2CC.  The patch fills the table
with a running 16-bit sum of a 512-byte page; the scanner below finds the block by decoding
candidate windows, so no offset has to be passed in.

    python np3_decode.py PAGE0.NP3 [PAGE1.NP3] [--hex N] [--ring N]

One file  -> decode and print the page header plus a hex preview of its data.
Two files -> merge by page number into the 864-byte state area and decode the pre-shoot
             STATUS fields and RING descriptors.

Page layout (16 B header + 432 B data + 64 B zero, all inside the 512 B table):
    +0x00 u32 magic   'Z6PD' (bytes 5A 36 50 44)
    +0x04 u16 version (1)
    +0x06 u8  page
    +0x07 u8  total   (2)
    +0x08 u16 len     (432)
    +0x0a u16 sum16   (16-bit sum of the 432 data bytes taken as little-endian words)
    +0x0c u32 seq     (save-action counter at export time)
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

CURVE_OFF = 0x40
PAGE_DATA = 432
STATUS_BYTES = 96
RING_BYTES = 768
NSLOT = 48

ST_FIELDS = (
    ('magic', 'u32'), ('count', 'u32'), ('write_idx', 'u32'), ('frozen', 'u32'),
    ('retained', 'u32'), ('triggers', 'u32'), ('armed', 'u32'), ('magic_inv', 'u32'),
    ('jpeg_hits', 'u32'), ('jpeg_slot', 'u32'), ('jpeg_addr', 'u32'), ('page', 'u32'),
    ('save_actions', 'u32'), ('np3_writes', 'u32'), ('np3_addr', 'u32'),
    ('rsv0', 'u32'), ('rsv1', 'u32'), ('rsv2', 'u32'), ('rsv3', 'u32'), ('rsv4', 'u32'),
    ('rsv5', 'u32'), ('rsv6', 'u32'), ('rsv7', 'u32'), ('rsv8', 'u32'),
)
SITE_NAMES = ['capseq-reg-entry', 'BufRelAl-entry', 'CapSt-pool-entry', 'CaptureService-A',
              'CaptureService-B', 'CaptureService-C', 'FrontEnd-A', 'CamCtl-InitiateCapture',
              'np3-save-action', 'np3-rev16']


def decode_block(chunk: bytes) -> bytes:
    prev, out = 0, bytearray()
    for i in range(256):
        w = int.from_bytes(chunk[2 * i:2 * i + 2], 'big')
        out += ((w - prev) & 0xFFFF).to_bytes(2, 'little')
        prev = w
    return bytes(out)


def find_page(data: bytes):
    """Locate the table: prefer +0x40, else scan every 2-byte window for 'Z6PD'."""
    cands = [CURVE_OFF] + list(range(0, max(0, len(data) - 512) + 1, 2))
    for off in cands:
        if off + 512 > len(data):
            continue
        page = decode_block(data[off:off + 512])
        if page[:4] == b'Z6PD':
            return off, page
    return None, None


def parse_page(page: bytes) -> dict:
    magic = struct.unpack_from('<I', page, 0)[0]
    ver, = struct.unpack_from('<H', page, 4)
    pg, total = page[6], page[7]
    ln, s16 = struct.unpack_from('<HH', page, 8)
    seq, = struct.unpack_from('<I', page, 12)
    data = page[16:16 + ln]
    want = sum(int.from_bytes(data[i:i + 2], 'little')
               for i in range(0, len(data) - 1, 2)) & 0xFFFF
    return dict(magic=magic, version=ver, page=pg, total=total, len=ln, sum16=s16,
                sum_ok=(s16 == want), seq=seq, data=data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--hex', type=int, default=64, help='bytes of page data to preview')
    ap.add_argument('--ring', type=int, default=48, help='max ring descriptors to print')
    args = ap.parse_args()

    pages = {}
    for f in args.files:
        raw = Path(f).read_bytes()
        off, page = find_page(raw)
        if page is None:
            print(f'{f}: no Z6PD page found (file {len(raw)} B)')
            continue
        p = parse_page(page)
        print(f'{Path(f).name}: {len(raw)} B, table at +{off:#x}, magic {p["magic"]:#010x}, '
              f'v{p["version"]}, page {p["page"]}/{p["total"]}, len {p["len"]}, '
              f'sum16 {p["sum16"]:#06x} {"ok" if p["sum_ok"] else "BAD"}, seq {p["seq"]}')
        if p['magic'] != 0x4450365A:
            print('  magic is not Z6PD -- not written by this patch')
            continue
        if args.hex:
            print(f'  data[0:{args.hex}] {p["data"][:args.hex].hex()}')
        pages[p['page']] = p

    if len(pages) < 2:
        if pages:
            print('\n(one page only; pass the other NP3 to decode STATUS/RING)')
        return 0 if pages else 1

    state = b''.join(pages[i]['data'] for i in sorted(pages))
    st = state[:STATUS_BYTES]
    ring = state[STATUS_BYTES:STATUS_BYTES + RING_BYTES]
    print(f'\nmerged {len(state)} B from pages {sorted(pages)}')
    print('STATUS')
    vals = struct.unpack_from('<%dI' % len(ST_FIELDS), st, 0)
    for (name, _), v in zip(ST_FIELDS, vals):
        print(f'  {name:14s} {v:#010x} ({v})')
    print('RING')
    shown = 0
    for i in range(min(NSLOT, args.ring)):
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
