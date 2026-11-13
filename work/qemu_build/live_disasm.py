"""Dump a live RAM range out of the running board and disassemble it.

The image on disk is not the runtime layout: 0x10020000-0x1011ffff reads as
zeros in fw_verified/eg1850_mas_01700 while the CPU is demonstrably executing
there. Anything at those addresses has to be recovered from the guest itself.

  python live_disasm.py --addr 0x10024500 --words 320 --at 45
  python live_disasm.py --addr 0x10024500 --words 320 --at 45 --arm
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_probe as bp

OBJDUMP = r"D:\z6ii_fw_lab\tools\msys64\ucrt64\bin\arm-none-eabi-objdump.exe"


def dump_words(hmp, addr, words, chunk=64):
    out = []
    for start in range(0, words, chunk):
        n = min(chunk, words - start)
        text = hmp.cmd("xp /%dxw 0x%08x" % (n, addr + start * 4))
        got = 0
        for line in text.splitlines():
            m = re.match(r"^\s*([0-9a-fA-F]{8}):(.*)$", line)
            if not m:
                continue
            for tok in m.group(2).split():
                if re.fullmatch(r"0x[0-9a-fA-F]{8}", tok):
                    out.append(int(tok, 16))
                    got += 1
        if not got:
            print("  [debug] raw reply for 0x%08x: %r" %
                  (addr + start * 4, text[-400:]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x10024500)
    ap.add_argument("--words", type=int, default=256)
    ap.add_argument("--at", type=float, default=45.0)
    ap.add_argument("--port-base", type=int, default=4950)
    ap.add_argument("--aux", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--arm", action="store_true", help="decode as ARM, not Thumb")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cmd = [c for c in json.load(open(bp.CMD))]
    cmd[0] = bp.QEMU
    if args.aux:
        for i, part in enumerate(cmd):
            if "master-boot-aux=" in part:
                cmd[i] = re.sub(r"master-boot-aux=.*?(?=,controller-flash-data=)",
                                "master-boot-aux=" + args.aux, part)
                break
    if "-monitor" in cmd:
        i = cmd.index("-monitor")
        del cmd[i:i + 2]
    for prop in [p for p in args.extra.split(",") if p]:
        cmd += ["-global", prop]
    hmp_port = args.port_base + 7
    cmd += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % args.port_base,
            "-monitor", "tcp:127.0.0.1:%d,server=on,wait=off" % hmp_port]

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\livedis.txt"), "wb")
    proc = subprocess.Popen(cmd, cwd=bp.ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=bp.env())
    t0 = time.time()
    try:
        hmp = bp.Hmp(hmp_port)
        listing = hmp.cmd("qom-list /machine/unattached")
        gpios = bp.instances(listing, "nikon-z72-controller-gpio")
        configs = bp.instances(listing, "nikon-z72-boot-config")
        wire = bp.Qtest(args.port_base)
        for bank, pin, level in bp.GPIO_INPUTS:
            wire.cmd("set_irq_in /machine/unattached/%s unnamed-gpio-in %d %d"
                     % (gpios[bank], pin, level))
        for level in (0, 1):
            wire.cmd("set_irq_in /machine/unattached/%s frame-sync-enable 0 %d"
                     % (configs[0], level))
        while time.time() - t0 < args.at:
            time.sleep(0.4)
        vals = dump_words(hmp, args.addr, args.words)
        print("read %d words from 0x%08x" % (len(vals), args.addr))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    blob = b"".join(v.to_bytes(4, "little") for v in vals)
    out = args.out or os.path.join(bp.ROOT, r".local\z62-smoke\livedis.bin")
    open(out, "wb").write(blob)
    mode = "arm" if args.arm else "thumb"
    txt = subprocess.run(
        [OBJDUMP, "-D", "-b", "binary", "-m", "arm",
         "-M", "force-" + mode, "--adjust-vma=0x%x" % args.addr, out],
        capture_output=True, text=True).stdout
    print(txt)


if __name__ == "__main__":
    main()
