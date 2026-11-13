"""Dump guest RAM to host files, then analyse offline.

The running image is not the disk image: 0x1002_xxxx and 0x1010_xxxx read as
zeros in fw_verified/eg1850_mas_01700 while the CPU executes there. Anything in
those windows - and any runtime-built table - only exists in the guest, so a
single snapshot is far cheaper than repeated gdb reads.

  python ram_dump.py --at 40 --out run/ram
  python ram_dump.py --at 40 --regions 0x10000000:0x02000000,0xc0000000:0x04000000
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


def dump_via_xp(hmp, base, size, path, chunk=1024):
    """Read through the CPU's address space.

    pmemsave() walks address_space_memory, but the master engine runs in its
    own private container, so pmemsave returns zeros for every engine window.
    xp goes through cpu_memory_rw_debug() and therefore sees what the guest
    sees.
    """
    got = 0
    with open(path, "wb") as out:
        for off in range(0, size, chunk * 4):
            n = min(chunk, (size - off) // 4)
            text = hmp.cmd("xp /%dxw 0x%08x" % (n, base + off))
            words = []
            for line in text.splitlines():
                m = re.match(r"^\s*([0-9a-fA-F]{8}):(.*)$", line)
                if not m:
                    continue
                for tok in m.group(2).split():
                    if re.fullmatch(r"0x[0-9a-fA-F]{8}", tok):
                        words.append(int(tok, 16))
            if len(words) != n:
                print("  short read at 0x%08x: %d/%d" % (base + off,
                                                         len(words), n))
            out.write(b"".join(w.to_bytes(4, "little") for w in words))
            got += len(words)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=40.0)
    ap.add_argument("--port-base", type=int, default=5160)
    ap.add_argument("--aux", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--out", default="run/ram")
    ap.add_argument("--regions", default="0x10000000:0x00140000")
    ap.add_argument("--pmemsave", action="store_true",
                    help="use pmemsave (system_memory only, misses engine views)")
    args = ap.parse_args()
    args.out = os.path.abspath(args.out)
    os.makedirs(args.out, exist_ok=True)

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

    log = open(os.path.join(bp.ROOT, r".local\z62-smoke\ramdump.txt"), "wb")
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
        for spec in [s for s in args.regions.split(",") if s]:
            addr, size = spec.split(":")
            addr = int(addr, 0)
            size = int(size, 0)
            name = "ram_%08x_%08x.bin" % (addr, size)
            path = os.path.join(args.out, name)
            if args.pmemsave:
                print("pmemsave 0x%08x 0x%08x -> %s" % (addr, size, name))
                hmp.cmd("pmemsave 0x%08x 0x%08x %s" % (addr, size, path))
            else:
                print("xp dump 0x%08x 0x%08x -> %s" % (addr, size, name))
                n = dump_via_xp(hmp, addr, size, path)
                print("  %d words" % n)
            print("  %d bytes" % (os.path.getsize(path)
                                  if os.path.exists(path) else -1))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
