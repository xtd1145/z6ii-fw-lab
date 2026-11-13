"""Boot patch containers without the HTTP console.

Reuses z62_webui.console.Machine in-process, so nothing depends on a background
server surviving between tool calls. Containers are decrypted offline first.

  python work/direct_containers.py --offset 0 --count 4
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\z6ii_fw_lab")
sys.path.insert(0, str(ROOT / "z62_webui"))
sys.path.insert(0, str(ROOT))
import console as C  # noqa: E402
import z6ii_patch as zp  # noqa: E402

OUT = ROOT / ".local" / "webui" / "variants_try.json"
EXT = ROOT / ".local" / "webui" / "extracted"
AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                for n, a in ((17, 0x132c0000), (18, 0x132e0000), (32, 0x13e80000)))
DEADLOOP = "101034fc"
MIN_GUEST = 2.4
MIN_UPLOADS = 250
MIN_RENDERED = 200
MIN_PIXELS = 30


def png_stats(path):
    data = Path(path).read_bytes()
    width = height = nonblack = 0
    if not data.startswith(b"\x89PNG"):
        return 0
    pos, idat = 8, bytearray()
    import struct
    import zlib
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
    stride = width * 3
    for y in range(height):
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        if y % 8 == 0:
            for x in range(0, stride, 3 * 61):
                if row[x:x + 3] != b"\0\0\0":
                    nonblack += 1
    return nonblack


def extract(src):
    raw, plain = zp.open_container(str(src), zp.load_key(str(ROOT / "key.json")))
    body, mods = zp.parse(plain)
    name, off, length = zp.find(mods, "eg1850_mas")
    EXT.mkdir(parents=True, exist_ok=True)
    dest = EXT / (Path(src).stem + ".bi")
    dest.write_bytes(bytes(body[off:off + length]))
    return dest


def boot(master, tag):
    m = C.Machine()
    cfg = dict(
        master=str(master),
        slave=str(ROOT / "fw_verified" / "eg1850_sla_01700"),
        controller=str(ROOT / "fw_verified" / "ex1850_012000.bi"),
        card=str(ROOT / ".local" / "sdcard.raw"),
        aux=AUX, profile="z62-dual", main_screen=True, logport=False,
        clear_status=True)
    started = m.start(cfg)
    if not started.get("ok"):
        return dict(pass_=False, error=str(started.get("error")))
    t0 = time.time()
    guest = None
    while time.time() - t0 < 240:
        time.sleep(3)
        if not m.running():
            m.stop()
            return dict(pass_=False, error="machine exited", wall=round(time.time() - t0, 1))
        guest = m.guest_seconds()
        if guest is not None and guest >= MIN_GUEST:
            break
    gpu = m.gpu_counters()
    pcs = {c["cpu"]: c["pc"] for c in m.cores()}
    path = m.shot()
    nonblack = png_stats(path) if path else 0
    m.stop()
    up = gpu.get("successful-uploads") or 0
    rnd = gpu.get("rendered-commands") or 0
    cpu0 = pcs.get(0)
    ok = ((guest or 0) >= MIN_GUEST and up >= MIN_UPLOADS and rnd >= MIN_RENDERED
          and nonblack > MIN_PIXELS and cpu0 != DEADLOOP)
    return dict(pass_=ok, guest=guest, uploads=up, rendered=rnd,
                nonblack=nonblack, cpu0=cpu0, wall=round(time.time() - t0, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--count", type=int, default=4)
    args = ap.parse_args()
    data = json.loads(OUT.read_text(encoding="utf-8"))
    boxes = [r for r in data["runs"] if r.get("kind") == "container"
             and not Path(r["path"]).is_absolute()]
    batch = boxes[args.offset:args.offset + args.count]
    print("batch", args.offset, len(batch), "of", len(boxes))
    for r in batch:
        src = ROOT / r["path"]
        t0 = time.time()
        try:
            dest = extract(src)
            print("extract", src.name, "->", dest.name, flush=True)
            res = boot(dest, r.get("tag"))
            res.update(kind="container", path=r["path"], extracted=str(dest))
        except Exception as exc:
            res = dict(kind="container", path=r["path"], pass_=False,
                       error="exception: %s" % exc)
        res["tag"] = r.get("tag")
        res["seconds"] = round(time.time() - t0, 1)
        r.update(res)
        data["summary"] = dict(
            pass_=sum(1 for x in data["runs"] if x.get("pass_")),
            total=len(data["runs"]))
        OUT.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print("%-10s %-4s guest=%-5s up=%-4s rnd=%-4s px=%-4s %s"
              % (res.get("tag"), "PASS" if res.get("pass_") else "FAIL",
                 res.get("guest"), res.get("uploads"), res.get("rendered"),
                 res.get("nonblack"), res.get("error") or ""), flush=True)
    print("== %d/%d pass" % (data["summary"]["pass_"], data["summary"]["total"]))


if __name__ == "__main__":
    main()
