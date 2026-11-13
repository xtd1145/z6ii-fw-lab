#!/usr/bin/env python3
"""Test K8 = seed XOR keystream(a,b,M) against a signed image header."""
import hashlib, sys, time

SEED = bytes.fromhex("0023002400250026")
HDR = 0x20


def ks(a, b, mul, n=8):
    out = bytearray()
    acc = b
    step = (a * mul) & 0xFFFFFFFF
    for _ in range(n):
        acc = (acc + step) & 0xFFFFFFFF
        out.append(acc & 0xFF)
        step = (step + a) & 0xFFFFFFFF
    return bytes(out)


def k8_table(mul):
    tbl = []
    for a in range(256):
        for b in range(256):
            tbl.append(bytes(x ^ y for x, y in zip(SEED, ks(a, b, mul))))
    return tbl


def probe(raw, dec, label, mul=0x260, starts=(0, 0x0c, 0x14, 0x20, 0x30, 0x40),
          endsubs=(0, 8, 0x10, 0x14, 0x20), seeds=None, verbose=True):
    want = raw[:20]
    N = len(dec)
    tbl = k8_table(mul)
    seeds = seeds or {"base": SEED}
    found = []
    t0 = time.time()
    for sname, sd in seeds.items():
        for st in starts:
            for es in endsubs:
                en = N - es
                if en <= st + 8:
                    continue
                body = dec[st:en]
                base = hashlib.sha1(body)
                for kb in tbl:
                    k8 = bytes(x ^ y for x, y in zip(sd, kb))
                    h = base.copy()
                    h.update(k8)
                    if h.digest() == want:
                        found.append((sname, st, en, k8.hex()))
                        print("HIT", label, sname, hex(st), hex(en), k8.hex())
    if verbose:
        print("%s: done 0x%x domains in %.1fs, hits=%d" %
              (label, len(starts) * len(endsubs), time.time() - t0, len(found)))
    return found


if __name__ == "__main__":
    raw = open("fw_set/Z6_2_0170.bin", "rb").read()
    dec = open("Z6_2_0170.dec", "rb").read()
    print("sizes", len(raw), len(dec))
    probe(raw, dec, "Z6_2_0170")
