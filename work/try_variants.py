"""Boot every patch package once and judge the HUD.

Masters in dist/ are used as-is; containers are extracted to their mas first
(the console's /api/variant action=extract pins the result). The pre-shoot
hook package from work/preshoot_build is tested first, then everything dist/
offers, grouped by SHA so identical files run once.

  python work/try_variants.py
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
PRESHOOT = ROOT / "work" / "preshoot_build" / "Z6_2_0170_hooks.bin"
STOCK = ROOT / "fw_verified" / "eg1850_mas_01700"
DEADLOOP = "101034fc"
MIN_GUEST = 2.4
MIN_UPLOADS = 250
MIN_RENDERED = 200
MIN_PIXELS = 30


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read()


def get_json(path):
    return json.loads(get(path))


def post(path, payload=None, timeout=300):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload or {}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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


def inventory():
    items, seen = [], {}
    for entry in get_json("/api/variants")["variants"]:
        key = entry["sha256"]
        if key in seen:
            seen[key]["files"].append(entry["path"])
            continue
        item = dict(kind=entry["kind"], path=entry["path"],
                    label=entry.get("label"), sha=key,
                    diff=entry.get("diff_bytes"), files=[entry["path"]])
        seen[key] = item
        items.append(item)
    masters = [i for i in items if i["kind"] == "master"]
    boxes = [i for i in items if i["kind"] == "container"]
    return masters, boxes


def boot(master_path, tag, note=""):
    pin = post("/api/variant", dict(path=master_path))
    if not pin.get("ok"):
        return dict(tag=tag, note=note, pass_=False, error="pin: %s" % pin)
    inv = get_json("/api/state")["inputs"]
    paths = {k: inv[k]["path"] for k in ("master", "slave", "controller", "card")
             if inv[k].get("present")}
    started = post("/api/start", dict(paths, profile="z62-dual",
                                      aux=inv["parameters"]["aux"],
                                      clear_status=True))
    if not started.get("ok"):
        return dict(tag=tag, note=note, pass_=False, error=str(started))
    t0 = time.time()
    state = None
    while time.time() - t0 < 220:
        time.sleep(4)
        state = get_json("/api/state")
        if not state["running"]:
            post("/api/stop")
            return dict(tag=tag, note=note, pass_=False, error="machine exited",
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
    return dict(tag=tag, note=note, pass_=ok, guest=state.get("guest"),
                uploads=up, rendered=rnd, nonblack=nonblack, cpu0=cpu0,
                png_sha=hashlib.sha256(png).hexdigest()[:12],
                wall=round(time.time() - t0, 1))


def main():
    masters, boxes = inventory()
    report = dict(started=time.strftime("%Y-%m-%d %H:%M:%S"), runs=[])
    done = set()
    if OUT.is_file():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            report["runs"] = old.get("runs", [])
            done = {(r.get("kind"), r.get("path")) for r in report["runs"]}
        except ValueError:
            pass
    plan = []
    if PRESHOOT.is_file():
        plan.append(("preshoot", "container", str(PRESHOOT), "预拍摄钩子包 (54ed5f2c)"))
    for m in masters:
        plan.append((m["label"] or "patched", "master", m["path"],
                     "diff=%s" % m.get("diff")))
    for b in boxes:
        plan.append((b["label"] or "container", "container", b["path"], ""))

    for label, kind, path, note in plan:
        if (kind, path) in done:
            print("%-10s %-9s skip (already in results)" % (label, kind),
                  flush=True)
            continue
        t0 = time.time()
        try:
            if kind == "container":
                info = post("/api/variant", dict(path=path, action="extract"))
                if not info.get("ok"):
                    result = dict(tag=label, kind=kind, path=path, note=note,
                                  pass_=False,
                                  error="extract: %s" % info.get("error"))
                else:
                    result = boot(info["path"], label, note)
                    result.update(kind=kind, path=path,
                                  extracted=info.get("path"))
            else:
                result = boot(path, label, note)
                result.update(kind=kind, path=path)
        except Exception as exc:
            result = dict(tag=label, kind=kind, path=path, note=note,
                          pass_=False, error="exception: %s" % exc)
        print("%-10s %-9s %-4s guest=%-5s up=%-4s rnd=%-4s px=%-4s %s"
              % (label, kind, "PASS" if result.get("pass_") else "FAIL",
                 result.get("guest"), result.get("uploads"),
                 result.get("rendered"), result.get("nonblack"),
                 result.get("error") or ""), flush=True)
        result["seconds"] = round(time.time() - t0, 1)
        report["runs"].append(result)
        report["summary"] = dict(
            pass_=sum(1 for r in report["runs"] if r.get("pass_")),
            total=len(report["runs"]))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, indent=1), encoding="utf-8")

    post("/api/variant", dict(path=str(STOCK)))
    report["summary"] = dict(
        pass_=sum(1 for r in report["runs"] if r.get("pass_")),
        total=len(report["runs"]))
    OUT.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("\n== %d/%d pass" % (report["summary"]["pass_"],
                               report["summary"]["total"]))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
