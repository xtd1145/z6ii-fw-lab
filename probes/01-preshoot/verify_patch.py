"""Container-level check of the shipped pre-shoot image, then A/B on the payload taken
straight out of that container.

Nothing here trusts the repacker: the container is decrypted from scratch, both CRC levels
and the header digest are recomputed, the mas payload is compared byte for byte with what
hookgen meant to write, and then the hook layer + core are re-proved by execution on the
module as it came out of the container.
"""
from __future__ import annotations

import binascii
import hashlib
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / 'z6ii_mem'))
import decrypt_firmware as fw                  # noqa: E402
import hookgen as H                            # noqa: E402
import test_core as T                          # noqa: E402

OUT = HERE / 'Z6_2_0170_preshoot_np3.bin'
K8 = bytes.fromhex(os.environ.get('Z6II_K8', ''))
if len(K8) != 8:
    raise SystemExit('set Z6II_K8 to the 8-byte signing key (never commit it)')
EXPECT_LEN = 49_002_888


def main():
    ok = True
    raw = OUT.read_bytes()
    plain = fw.decrypt(raw, *fw.load_key(str(HERE.parents[1] / 'key.json')))
    body = plain[fw.HDR:]
    digest = hashlib.sha1(body + K8).digest()
    print(f'container   {len(raw)} B (expect {EXPECT_LEN})')
    print(f'signature   SHA1(body||K8) {digest.hex()} '
          f'{"MATCH" if digest == plain[:20] else "MISMATCH"}')
    ok &= len(raw) == EXPECT_LEN and digest == plain[:20]

    mods = fw.validate_firmware(memoryview(body))
    pkg = struct.unpack_from('>H', body, len(body) - fw.TRAILER)[0]
    pkg_want = binascii.crc_hqx(bytes(body[:-fw.TRAILER]), 0)
    print(f'structure   {len(mods)} modules validated, package CRC {pkg:04x} '
          f'{"OK" if pkg == pkg_want else "BAD"}')
    ok &= pkg == pkg_want

    name, off, ln = next(m for m in mods if m[0].startswith('eg1850_mas'))
    mas = bytes(body[off:off + ln - 2])
    writes, _ = H.build()
    want = H.apply(writes)
    print(f'mas         {name}: {ln - 2} B, payload byte-exact with generator: {mas == want}')
    ok &= mas == want

    # ---- execution proof on the payload as shipped ----------------------------
    st = T.states(8, seed=99)
    stock = T.Runner(H.MAS)
    nocore = bytearray(mas)
    nocore[H.CTRL:H.CTRL + 16] = b'\0' * 16
    nz = T.Runner(bytes(nocore))
    live = T.Runner(mas)

    a_bad = b_bad = 0
    for nm, site, win in H.SITES:
        for init in st:
            ra = stock.call(site, win, init)
            for runner, bucket in ((nz, 'A'), (live, 'B1')):
                rb = runner.call(site, win, init)
                good, why = T.same_regs(ra, rb)
                if not good and not ra['err']:
                    if bucket == 'A':
                        a_bad += 1
                    else:
                        b_bad += 1
    total = len(H.SITES) * len(st)
    print(f'A  no core   {total - a_bad}/{total} identical to stock')
    print(f'B1 core live {total - b_bad}/{total} identical to stock (ABI)')
    ok &= a_bad == 0 and b_bad == 0

    r = T.Runner(mas)
    n = len(H.SITES)
    REC = [k for k in range(n)
           if k not in (H.TRIGGER_SLOT, H.IO_SLOT)]
    for k in REC:
        r.call(H.SITES[k][1], H.SITES[k][2], {'r0': 0xA000 + k, 'r1': 0xB000 + k,
                                              'lr': 0xC000 + k})
    s = r.status()
    r.call(H.SITES[H.TRIGGER_SLOT][1], H.SITES[H.TRIGGER_SLOT][2],
           {'r0': 0xDEAD, 'lr': 0xD000})
    s2 = r.status()
    b2 = (s['magic'] == H.SMAGIC and s['armed'] == 1 and s['count'] == len(REC)
          and s2['frozen'] == 1 and s2['retained'] == min(len(REC), H.NSLOT)
          and s2['triggers'] == 1 and s2['count'] == len(REC))
    print(f'B2 state     magic={s["magic"]:#x} armed={s["armed"]} count={s["count"]} -> '
          f'frozen={s2["frozen"]} retained={s2["retained"]} '
          f'{"OK" if b2 else "FAIL"}')
    ok &= b2

    print(f'\nRESULT  {"all checks pass" if ok else "PROBLEM"}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
