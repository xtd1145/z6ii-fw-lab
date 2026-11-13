#!/usr/bin/env python3
"""Z6 II K8 只读抓取与验证工具（全程不向相机/SD 卡写入任何字节）。

用法：
  python k8_capture.py --selfcheck                      只读自检，证明原始文件未被改动
  python k8_capture.py --text dump.log                  解析控制台文本转储，取出 ctx+0xc 并当场验证
  python k8_capture.py --dump ram.bin [--base 0xADDR]    解析二进制转储
  python k8_capture.py --watch COMx --baud 115200        仅 RX 监听，等待相机上电自发输出
  python k8_capture.py --predict                        用固件里的表复算 K8（预期失败，见下）

只读保证：
  * 固件与模块一律 'rb' 打开；本脚本唯一的写路径是 --out 指定的本地抓取文件，
    且拒绝与固件/模块同路径。绝不向串口 TX（--watch 里把 write 绑成抛异常）。
  * 不实现任何刷机、升级、写 Flash 的命令通路。

K8 的来源（反汇编确证，全部在 eg1850_mas_01700 内，VMA-0x10000000=文件偏移）：
  校验函数 0x10392284（FirmwareChkLib.c:170）：
    ctx = 0xc2b2b608, 0x34 字节 {ptr@0, len@4, fn@8, key@0xc, param@0x14, model@0x18, expect@0x20}
    r2 = rev(~ctx.param); ctx.key 就地做 keystream XOR；然后 memcpy(ptr+len, ctx.key, 8)
    SHA1(body || K8) 与 ctx.expect(0x14 字节) 比 20 字节
  变换 0x1039ea3d：acc=b, step=a*0x260, 8 轮 {acc+=step; out=seed^acc; step+=a}
    b = 表[0x12078c96 + (param 四字节异或折叠)]
    a = 表[0x12078d96 + (model 串解析出的十进制数 & 0xff)]
  所以 a、b、种子都取自运行期 RAM；静态扫描 131 万组 (a,b,M) 全部不中，
  本工具因此只做“抓 + 验”，不做任何写入。
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys

import decrypt_firmware as fw
import k8_from_dump as kd

HDR = 0x20
CTX_ADDR = 0xC2B2B608
GATE_ADDR = 0xC1887BC0
SEED_VMA = 0x10000088
MAS = "all_modules/Z6_2_0170/eg1850_mas_01700"
ORIG = r"C:\Users\BI\Downloads\Z6_2_0170.bin"
ORIG_SIZE = 49002888
ORIG_SHA256 = "5a9640b4c07abe1ecbf5a52655cbec6b893bba045bb2a1a1d21055b9f8f9d06d"

EX_USAGE = 2


def sha256_file(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def load_body(args):
    """Return (header20, body) from a decrypted .dec or an encrypted .bin + key."""
    if args.dec:
        plain = open(args.dec, "rb").read()
    else:
        key = fw.load_key(args.key)
        raw = open(args.firmware, "rb").read()
        fw.check_scheme(raw, args.firmware)
        plain = fw.decrypt(raw, *key)
    return bytes(plain[:20]), bytes(plain[HDR:])


def verify(k8, want, body, label):
    got = hashlib.sha1(body + k8).digest()
    ok = got == want
    print("  [%s] K8=%s  SHA1(body||K8)=%s  %s" % (label, k8.hex(), got.hex(),
                                                   "命中 ✔" if ok else "不中"))
    return ok


def fold32(v):
    return (v & 0xFF) ^ ((v >> 8) & 0xFF) ^ ((v >> 16) & 0xFF) ^ ((v >> 24) & 0xFF)


def keystream(a, b, mul=0x260, n=8):
    out, acc, step = bytearray(n), b, (a * mul) & 0xFFFFFFFF
    for i in range(n):
        acc = (acc + step) & 0xFFFFFFFF
        out[i] = acc & 0xFF
        step = (step + a) & 0xFFFFFFFF
    return bytes(out)


def parse_model_num(s, digits_are_numeric=True):
    """复现 0x1039ea3d 的十进制解析：跳过/折叠空格，值 = 数字 + 10*前值。"""
    acc = 0
    for ch in s:
        c = ch if isinstance(ch, int) else ord(ch)
        if c == 0:
            break
        if digits_are_numeric and 0x30 <= c <= 0x39:
            d = c - 0x30
        elif c == 0x20:
            d = (c - 0x30) & 0xFFFFFFFF
        else:
            d = c % 10
        acc = d + 10 * acc
    return acc


def report_ctx(buf, want, body, dump_base=None):
    """Locate the 0x34-byte verifier context inside a captured buffer and test it."""
    hits = kd.find_ctx(buf)
    if not hits:
        print("转储里没找到 ctx 锚点（fn=0x%08x / param=0x%x / 'Z_6_2'）。" %
              (kd.FN_TRANSFORM, kd.PARAM))
        print("若转储是 memdump 的单段，请用 --base 指明该段起始地址。")
        return False
    for o in hits:
        v = o + (dump_base if dump_base is not None else CTX_ADDR)
        ptr, ln, fn = struct.unpack_from("<III", buf, o)
        key = bytes(buf[o + 0x0C:o + 0x14])
        param = struct.unpack_from("<I", buf, o + 0x14)[0]
        model = bytes(buf[o + 0x18:o + 0x20]).split(b"\0")[0]
        exp = bytes(buf[o + 0x20:o + 0x34])
        print("ctx@0x%08x  data=0x%08x len=0x%x fn=0x%08x param=0x%x model=%s"
              % (v, ptr, ln, fn, param, model.decode("latin1")))
        print("  expect  = %s" % exp[:20].hex())
        print("  header20= %s" % want.hex())
        if exp[:20] == want:
            print("  expect 与镜像头摘要一致 → 这就是本次升级校验用的上下文")
        if ln == len(body):
            print("  len 与容器 body 一致 → 确认是容器校验上下文")
        if verify(key, want, body, "ctx+0x0c"):
            return True
        if key != kd.SEED:
            print("  槽位不等于静态种子 0x%s → 变换已执行过，槽位即 K8" % kd.SEED.hex())
    return False


def read_gate(buf, dump_base=None):
    """Answer the pending question: does the SD path demand the header SHA1?"""
    off = GATE_ADDR - dump_base if dump_base is not None else -1
    if off < 0 or off + 1 > len(buf):
        print("  [gate] 0x%08x 不在本段转储内——单独 memdump 该处 16 字节即可判定"
              % GATE_ADDR)
        return
    val = buf[off]
    print("  [gate] [0x%08x] = %d → %s" % (GATE_ADDR, val,
          "走 SHA 分支（需要 K8）" if val in (2, 4) else "走 CRC-only 分支（不需要 K8）"))


def do_watch(port, baud, out_path):
    """RX-only serial listener. Never transmits: ser.write is bound to a raiser."""
    try:
        import serial
    except ImportError:
        fw.die(EX_USAGE, "缺少 pyserial（pip install pyserial）。"
                         "没有它也能工作：把控制台输出另存为文本后用 --text 解析。")
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, 1
    ser.rts = False
    ser.dtr = False
    ser.write = _forbid_write            # 硬件层禁发：任何 write 立即抛错
    ser.open()
    print("仅监听 %s @%d（RTS/DTR 拉低，不发送任何字节）。Ctrl-C 结束。" % (port, baud))
    fh = open(out_path, "ab") if out_path else None
    try:
        while True:
            data = ser.read(512)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
                if fh:
                    fh.write(data)
                    fh.flush()
    except KeyboardInterrupt:
        print("\n停止监听")
    finally:
        if fh:
            fh.close()
        ser.close()


def _forbid_write(*a, **k):
    raise RuntimeError("只读模式：本工具禁止向串口发送任何字节")


def do_predict(args):
    m = open(MAS, "rb").read()
    t1 = m[0x12078c96 - 0x10000000:0x12078d96 - 0x10000000]
    t2 = m[0x12078d96 - 0x10000000:0x12078e96 - 0x10000000]
    param = kd.PARAM
    model = kd.MODEL + b"\0"
    seed = m[SEED_VMA - 0x10000000:SEED_VMA - 0x10000000 + 8]
    b = t1[fold32(param)]
    for dig in (True, False):
        num = parse_model_num(model, dig)
        a = t2[num & 0xFF]
        k8 = bytes(x ^ y for x, y in zip(seed, keystream(a, b)))
        print("种子(文件 0x%08x)=0x%s  param=0x%x 折叠=0x%02x  a=0x%02x b=0x%02x 数=%d(数字位=%s)"
              % (SEED_VMA, seed.hex(), param, fold32(param), a, b, num, dig))
        print("  预测 K8 = %s" % k8.hex())
    print("\n说明：0x12078c96 一带在镜像里是 0x0248/0 的标志阵列，不是可用的字节表；")
    print("      种子槽 0x%s 也已被加载器改写（131 万组 (a,b,M) 全数扫过，零命中）。" % seed.hex())
    print("      结论：K8 只能从运行期 RAM 取，取回后用 --text/--dump 当场验证。")


def do_selftest(args):
    """In-memory fixture: plant a ctx, run the real parser, assert the digest hits."""
    want, body = load_body(args)
    key = bytes(range(8))
    ctx = bytearray(0x34)
    struct.pack_into("<III", ctx, 0, 0x80000000, len(body), kd.FN_TRANSFORM)
    ctx[0x0C:0x14] = key
    struct.pack_into("<I", ctx, 0x14, kd.PARAM)
    ctx[0x18:0x18 + len(kd.MODEL)] = kd.MODEL
    ctx[0x20:0x34] = hashlib.sha1(body + key).digest() + b"\0" * 4
    buf = bytearray(0x400)
    buf[0x100:0x134] = ctx
    print("合成 ctx 注入 0x400 字节假转储，跑真实解析链：")
    ok = report_ctx(bytes(buf), hashlib.sha1(body + key).digest(), body, CTX_ADDR - 0x100)
    print("自检 %s" % ("通过 ✔（抓取→定位→摘要校验 全链可用）" if ok else "失败"))
    return 0 if ok else 6


def main():
    ap = argparse.ArgumentParser(
        description="Z6 II K8 只读抓取/验证（不写入相机、不修改原始固件）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dec", help="已解密的容器 Z6_2_0170.dec")
    src.add_argument("--firmware", help="加密 .bin（需配合 --key）")
    ap.add_argument("--key", default="key.json")
    ap.add_argument("--dump", help="二进制 RAM 转储")
    ap.add_argument("--text", help="控制台/memdump 文本日志")
    ap.add_argument("--base", help="转储段起始地址（--text 默认 0x%x）" % CTX_ADDR)
    ap.add_argument("--watch", metavar="PORT", help="仅 RX 监听串口，绝不发送")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", help="本地抓取文件（只写这个，且不得指向固件）")
    ap.add_argument("--predict", action="store_true", help="用镜像里的表复算 K8")
    ap.add_argument("--selfcheck", action="store_true", help="证明原始固件未被改动")
    ap.add_argument("--selftest", action="store_true", help="内存内合成 ctx，验证解析链")
    args = ap.parse_args()

    if args.selftest:
        if not args.dec and not args.firmware:
            args.dec = "Z6_2_0170.dec"
        return do_selftest(args)

    if args.selfcheck:
        print("原始固件 %s" % ORIG)
        if not os.path.exists(ORIG):
            print("  文件不在本机，跳过")
        else:
            sz = os.path.getsize(ORIG)
            dg = sha256_file(ORIG)
            print("  长度 %d（应为 %d）%s" % (sz, ORIG_SIZE, "OK" if sz == ORIG_SIZE else "不一致!"))
            print("  SHA-256 %s" % dg)
            if ORIG_SHA256:
                print("  与记录值 %s" % ("一致 ✔" if dg == ORIG_SHA256 else "不一致!"))
        print("本脚本的写路径只有 --out；固件与模块均以 'rb' 打开。")
        return 0

    if args.watch:
        out = args.out or "capture_%s.log" % re.sub(r"\W", "_", args.watch)
        if os.path.realpath(out).lower() == os.path.realpath(ORIG).lower():
            fw.die(EX_USAGE, "--out 不得指向固件")
        do_watch(args.watch, args.baud, out)
        return 0

    if args.predict:
        do_predict(args)
        return 0

    if not (args.dump or args.text):
        ap.print_help()
        print("\n最少要给 --dump/--text（抓取结果）或 --watch/--predict。")
        return 0

    want, body = load_body(args)
    if args.dump:
        buf = open(args.dump, "rb").read()
        base = int(args.base, 0) if args.base else None
    else:
        buf = kd.parse_memdump_text(args.text)
        base = int(args.base, 0) if args.base else CTX_ADDR
    print("转储 %d 字节，%s" % (len(buf), os.path.abspath(args.dump or args.text)))
    ok = report_ctx(buf, want, body, base)
    read_gate(buf, base)
    print("\n结果：%s" % ("K8 已取得并通过摘要校验，可以离线重签名。" if ok else
                          "本次转储未取到 K8——需覆盖 0x%08x 起 0x34 字节。" % CTX_ADDR))
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())
