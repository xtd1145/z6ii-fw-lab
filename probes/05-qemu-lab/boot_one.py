"""Boot one patch package in the QEMU dual lab and judge the HUD.

Accepts an update container (49,002,888 B), a mas image (38,750,138 B) or an
already-extracted .bi. Uses z62_webui.console.Machine in-process so no HTTP
server is needed.

  python work/boot_one.py <path> [--tag name]
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\z6ii_fw_lab")
sys.path.insert(0, str(ROOT / "z62_webui"))
sys.path.insert(0, str(ROOT))
import console as C  # noqa: E402
import z6ii_patch as zp  # noqa: E402

AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                for n, a in ((17, 0x132c0000), (18, 0x132e0000), (32, 0x13e80000)))
DEADLOOP = "101034fc"
MASTER = 38750138
CONTAINER = 49002888
EXT = ROOT / ".local" / "webui" / "extracted"


def png_nonblack(path):
    import struct
    import zlib
    data = Path(path).read_bytes()
    if not data.startswith(b"\x89PNG"):
        return 0
    pos, idat, width, height = 8, bytearray(), 0, 0
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return 0
    stride, hits = width * 3, 0
    for y in range(height):
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        if y % 8 == 0:
            for x in range(0, stride, 3 * 61):
                if row[x:x + 3] != b"\0\0\0":
                    hits += 1
    return hits


def to_mas(src):
    size = src.stat().st_size
    if size == MASTER:
        return src
    if size != CONTAINER:
        raise SystemExit("unexpected size %d: %s" % (size, src))
    raw, plain = zp.open_container(str(src), zp.load_key(str(ROOT / "key.json")))
    body, mods = zp.parse(plain)
    name, off, length = zp.find(mods, "eg1850_mas")
    EXT.mkdir(parents=True, exist_ok=True)
    dest = EXT / (src.stem + ".bi")
    dest.write_bytes(bytes(body[off:off + length]))
    print("extracted", name, length, "->", dest)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    src = Path(args.path)
    master = to_mas(src)
    m = C.Machine()
    cfg = dict(master=str(master),
               slave=str(ROOT / "fw_verified" / "eg1850_sla_01700"),
               controller=str(ROOT / "fw_verified" / "ex1850_012000.bi"),
               card=str(ROOT / ".local" / "sdcard.raw"),
               aux=AUX, profile="z62-dual", main_screen=True, logport=False,
               clear_status=True)
    ok = m.start(cfg)
    if not ok.get("ok"):
        print("START FAIL", ok.get("error"))
        return
    t0 = time.time()
    guest = None
    while time.time() - t0 < 240:
        time.sleep(3)
        if not m.running():
            print("EXITED early after %.1fs" % (time.time() - t0))
            return
        guest = m.guest_seconds()
        if guest is not None and guest >= 2.4:
            break
    gpu = m.gpu_counters()
    pcs = {c["cpu"]: c["pc"] for c in m.cores()}
    shot = m.shot()
    px = png_nonblack(shot) if shot else 0
    m.stop()
    up = gpu.get("successful-uploads") or 0
    rnd = gpu.get("rendered-commands") or 0
    cpu0 = pcs.get(0)
    passed = ((guest or 0) >= 2.4 and up >= 250 and rnd >= 200 and px > 30
              and cpu0 != DEADLOOP)
    print("%s %s guest=%s up=%s rnd=%s px=%s cpu0=%s wall=%.1fs"
          % (args.tag or src.name, "PASS" if passed else "FAIL", guest, up,
             rnd, px, cpu0, time.time() - t0))


if __name__ == "__main__":
    main()
