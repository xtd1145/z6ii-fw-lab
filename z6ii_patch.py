#!/usr/bin/env python3
"""Z6 II (eg1850) 容器等长载荷补丁器 —— 只读原始固件，只写副本。

容器规范（本机已实测）：
  file[0x00:0x14]  SHA1(body || K8) 头部摘要（需要 K8，未重新签名时会失配）
  file[0x14:0x20]  12 字节保留，原样复制
  body = file[0x20:]
  body[0x00:0x20]  32 个空格标签
  body[0x20:0x24]  BE32 模块数
  body[0x30 + 32*i] 目录项 = {name[16], BE32 off, BE32 len, 8×0}，off/len 相对 body
  每个模块尾部 2 字节 = BE16 crc_hqx(module[:-2], 0)
  body 尾 16 字节   = BE16 crc_hqx(body[:-16], 0) + 14×0

因为两个 CRC 都是无密钥校验，载荷改完后可以完全自洽；唯一无法补的是头部摘要。
"""
import argparse
import binascii
import hashlib
import json
import os
import struct
import sys

import decrypt_firmware as fw

HDR = fw.HDR
TRAILER = fw.TRAILER
KEYJSON = "key.json"


def load_key(path=KEYJSON):
    return fw.load_key(path)


def open_container(path, key):
    raw = open(path, "rb").read()
    fw.check_scheme(raw, path)
    plain = fw.decrypt(raw, *key)      # xor 方案：同一函数既是解密也是再加密
    return raw, plain


def parse(plain):
    body = bytearray(plain[HDR:])
    assert body[:32] == b" " * 32, "body 标签不是 32 个空格，容器方案不符"
    n = struct.unpack_from(">I", body, 0x20)[0]
    mods = []
    for i in range(n):
        e = body[0x30 + 32 * i:0x30 + 32 * (i + 1)]
        name = e[:16].rstrip(b"\0").decode()
        off, ln = struct.unpack_from(">II", e, 16)
        mods.append([name, off, ln])
    return body, mods


def check_crcs(body, mods, verbose=True):
    ok = True
    for name, off, ln in mods:
        m = body[off:off + ln]
        want = binascii.crc_hqx(bytes(m[:-2]), 0)
        got = struct.unpack_from(">H", m, ln - 2)[0]
        if verbose:
            print(f"  {name:22s} off={off:#09x} len={ln:#09x} crc={got:04x} {'OK' if got == want else f'BAD({want:04x})'}")
        ok &= got == want
    pgot = struct.unpack_from(">H", body, len(body) - TRAILER)[0]
    pwant = binascii.crc_hqx(bytes(body[:-TRAILER]), 0)
    if verbose:
        print(f"  package crc={pgot:04x} computed={pwant:04x} {'OK' if pgot == pwant else 'BAD'}")
    return ok and pgot == pwant


def reseal(body, mods):
    """重算 5 个模块 CRC 和包 CRC（都不需要密钥）。返回包 CRC 是否变化。"""
    old = struct.unpack_from(">H", body, len(body) - TRAILER)[0]
    for name, off, ln in mods:
        struct.pack_into(">H", body, off + ln - 2, binascii.crc_hqx(bytes(body[off:off + ln - 2]), 0))
    struct.pack_into(">H", body, len(body) - TRAILER, binascii.crc_hqx(bytes(body[:-TRAILER]), 0))
    return old, struct.unpack_from(">H", body, len(body) - TRAILER)[0]


def find(mods, want):
    hit = [m for m in mods if m[0] == want] or [m for m in mods if m[0].startswith(want)]
    if len(hit) != 1:
        raise SystemExit(f"模块名 {want!r} 匹配到 {len(hit)} 个：{[m[0] for m in hit]}")
    return hit[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firmware", default=r"C:\Users\BI\Downloads\Z6_2_0170.bin")
    ap.add_argument("--key", default=KEYJSON)
    ap.add_argument("--out")
    ap.add_argument("--patch-module", action="append", default=[], metavar="MODULE:OFF:HEX")
    ap.add_argument("--k8", help="8 字节签名密钥（16 个 hex），用于重算头部摘要")
    ap.add_argument("--selftest", action="store_true", help="零改动重打包，必须与原始文件逐字节相同")
    ap.add_argument("--probe", action="store_true", help="改 mas 一个字节，观察模块 CRC/包 CRC 传播")
    args = ap.parse_args()
    if args.k8 is not None:
        try:
            kb = bytes.fromhex(args.k8)
        except ValueError:
            raise SystemExit("--k8 必须是 16 个 hex 字符（8 字节）")
        if len(kb) != 8:
            raise SystemExit("--k8 必须是 8 字节（16 个 hex）")

    key = load_key(args.key)
    raw, plain = open_container(args.firmware, key)
    body, mods = parse(plain)
    print(f"[容器] {args.firmware}  {len(raw)} 字节，{len(mods)} 个模块")
    if not check_crcs(body, mods):
        raise SystemExit("原始容器 CRC 校验不通过 —— 停止。")

    if args.selftest:
        out = reseal_and_encrypt(body, mods, plain, raw, key, args.k8)
        diff = sum(a != b for a, b in zip(out, raw))
        print(f"\n[selftest] 重打包与原始文件差异字节数 = {diff}（应为 0）")
        return 0 if diff == 0 else 8

    if args.probe:
        m = find(mods, "eg1850_mas")
        off = 0x100000
        old = body[m[1] + off]
        body[m[1] + off] = old ^ 0xFF
        b, mm = body, mods
        before = struct.unpack_from(">H", b, len(b) - TRAILER)[0]
        mc_before = struct.unpack_from(">H", b, m[1] + m[2] - 2)[0]
        after = reseal(b, mm)
        print(f"\n[probe] mas+{off:#x}: {old:02x} -> {old ^ 0xFF:02x}")
        print(f"  mas 模块 CRC   {mc_before:04x} -> {binascii.crc_hqx(bytes(b[m[1]:m[1]+m[2]-2]),0):04x}")
        print(f"  包 CRC         {after[0]:04x} -> {after[1]:04x}")
        print(f"  头部摘要       未重签名：{plain[:20].hex()}")
        print("  结论：改载荷只需修模块 CRC；包 CRC 会变但无密钥即可重算；只有头部摘要需要 K8。")
        return 0

    if not args.patch_module or not args.out:
        raise SystemExit("需要 --patch-module MODULE:OFF:HEX 和 --out（或 --selftest / --probe）")
    if os.path.realpath(args.out) == os.path.realpath(args.firmware):
        raise SystemExit("--out 不能是原始固件路径。")

    edits = []
    for spec in args.patch_module:
        name, offs, hexs = spec.split(":", 2)
        m = find(mods, name)
        off = int(offs, 0)
        data = bytes.fromhex(hexs)
        if off < 0 or off + len(data) > m[2] - 2:
            raise SystemExit(f"补丁越界：{name}+{off:#x}+{len(data)}")
        cur = bytes(body[m[1] + off:m[1] + off + len(data)])
        print(f"[补丁] {name} +{off:#x}: {cur.hex()} -> {data.hex()}")
        body[m[1] + off:m[1] + off + len(data)] = data
        edits.append((name, off, len(data)))

    out = reseal_and_encrypt(body, mods, plain, raw, key, args.k8)
    open(args.out, "wb").write(out)
    print(f"\n[写出] {args.out}  {len(out)} 字节")
    print(f"  与原始文件差异字节数 = {sum(a != b for a, b in zip(out, raw))}")
    if args.k8:
        print(f"  头部摘要（已用 K8={args.k8} 重签）= {hashlib.sha1(bytes(body) + bytes.fromhex(args.k8)).hexdigest()}")
    else:
        print(f"  头部摘要（旧，未重签名）= {plain[:20].hex()}")
        print(f"  新 body 需要的摘要       = SHA1(body || K8) —— 缺 K8，无法自签")
    # 重新解密自身，验证自洽
    raw2, plain2 = open_container(args.out, key)
    body2, mods2 = parse(plain2)
    print("  副本再解密 + CRC 链校验:", "通过" if check_crcs(body2, mods2, verbose=False) else "失败")
    return 0


def reseal_and_encrypt(body, mods, plain, raw, key, k8=None):
    reseal(body, mods)
    if k8 is not None:
        hdr = hashlib.sha1(bytes(body) + bytes.fromhex(k8)).digest()
    else:
        hdr = plain[:20]
    newplain = hdr + plain[20:HDR] + bytes(body)
    cipher = fw.decrypt(newplain, *key)   # xor 自反：再加密得到可直接落卡的密文
    return cipher


if __name__ == "__main__":
    sys.exit(main())
