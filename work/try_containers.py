"""Extract the remaining update containers offline, then boot each mas.

The console's online extract proved fragile under repeated use, so containers
are decrypted here (z6ii_patch) and only the boot goes through the console.
"""
import hashlib
import json
import struct
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(r"D:\z6ii_fw_lab")
BASE = "http://127.0.0.1:8642"
OUT = ROOT / ".local" / "webui" / "variants_try.json"
EXT = ROOT / ".local" / "webui" / "extracted"
DEADLOOP = "101034fc"
MIN_GUEST = 2.4
MIN_UPLOADS = 250
MIN_RENDERED = 200
MIN_PIXELS = 30


def _retry(fn, *a, **kw):
    last = None
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except Exception as exc:
            last = exc
            time.sleep(3)
    raise last


def get(path, timeout=120):
    def once():
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.read()
    return _retry(once)


def get_json(path):
    return json.loads(get(path))


def post(path, payload=None, timeout=300):
    def once():
        req = urllib.request.Request(BASE + path, method="POST",
                                     data=json.dumps(payload or {}).encode())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return _retry(once)


def png_stats(data):
    if not data.startswith(b"\x89PNG"):
        return 0, 0, 0
    pos, width, height, idat = 8, 0, 0, bytearray()
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
        return width, height, 0
    stride = width * 3
    nonblack = 0
    for y in range(height):
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        if y % 8 == 0:
            for x in range(0, stride, 3 * 61):
                if row[x:x + 3] != b"\0\0\0":
                    nonblack += 1
    return width, height, nonblack


def extract(src):
    import sys
    sys.path.insert(0, str(ROOT))
    import z6ii_patch as zp
    raw, plain = zp.open_container(str(src), zp.load_key(str(ROOT / "key.json")))
    body, mods = zp.parse(plain)
    name, off, length = zp.find(mods, "eg1850_mas")
    data = bytes(body[off:off + length])
    EXT.mkdir(parents=True, exist_ok=True)
    dest = EXT / (Path(src).stem + ".bi")
    dest.write_bytes(data)
    return dest, name, length


def boot(master):
    post("/api/stop")
    time.sleep(1.5)
    inv = get_json("/api/state")["inputs"]
    paths = {k: inv[k]["path"] for k in ("slave", "controller", "card")
             if inv[k].get("present")}
    paths["master"] = str(master)
    started = post("/api/start", dict(paths, profile="z62-dual",
                                      aux=inv["parameters"]["aux"],
                                      clear_status=True))
    if not started.get("ok"):
        return dict(pass_=False, error=str(started))
    t0 = time.time()
    state = None
    while time.time() - t0 < 220:
        time.sleep(4)
        state = get_json("/api/state")
        if not state["running"]:
            return dict(pass_=False, error="machine exited",
                        guest=state.get("guest"),
                        wall=round(time.time() - t0, 1))
        if (state.get("guest") or 0) >= MIN_GUEST:
            break
    pcs = {c["cpu"]: c["pc"] for c in get_json("/api/regs")["cores"]}
    post("/api/shot")
    png = get("/screen.png")
    _, _, nonblack = png_stats(png)
    post("/api/stop")
    gpu = state.get("gpu") or {}
    up = gpu.get("successful-uploads") or 0
    rnd = gpu.get("rendered-commands") or 0
    cpu0 = pcs.get(0)
    ok = ((state.get("guest") or 0) >= MIN_GUEST and up >= MIN_UPLOADS
          and rnd >= MIN_RENDERED and nonblack > MIN_PIXELS
          and cpu0 != DEADLOOP)
    return dict(pass_=ok, guest=state.get("guest"), uploads=up, rendered=rnd,
                nonblack=nonblack, cpu0=cpu0,
                png_sha=hashlib.sha256(png).hexdigest()[:12],
                wall=round(time.time() - t0, 1))


def main():
    data = json.loads(OUT.read_text(encoding="utf-8"))
    todo = [r for r in data["runs"]
            if r.get("kind") == "container"
            and not Path(r["path"]).is_absolute()]
    print("containers to redo:", len(todo))
    for r in todo:
        src = Path(r["path"]) if Path(r["path"]).is_absolute() else ROOT / r["path"]
        t0 = time.time()
        try:
            dest, name, length = extract(src)
            print("extract", src.name, "->", dest.name, name, length, flush=True)
            res = boot(dest)
            res.update(kind="container", path=r["path"], extracted=str(dest))
        except Exception as exc:
            res = dict(kind="container", path=r["path"], pass_=False,
                       error="exception: %s" % exc)
        res["tag"] = r.get("tag")
        res["seconds"] = round(time.time() - t0, 1)
        r.update(res)
        print("%-10s %-4s guest=%-5s up=%-4s rnd=%-4s px=%-4s %s"
              % (res.get("tag"), "PASS" if res.get("pass_") else "FAIL",
                 res.get("guest"), res.get("uploads"), res.get("rendered"),
                 res.get("nonblack"), res.get("error") or ""), flush=True)
        data["summary"] = dict(
            pass_=sum(1 for x in data["runs"] if x.get("pass_")),
            total=len(data["runs"]))
        OUT.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print("== %d/%d pass" % (data["summary"]["pass_"], data["summary"]["total"]))


if __name__ == "__main__":
    main()
