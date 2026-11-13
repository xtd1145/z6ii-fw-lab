"""Soak every bootable firmware variant ten times and judge the UI.

Variants are grouped by SHA-256: several files in dist/ are byte-identical, and
re-running the same bytes ten times proves nothing extra. Each group runs N
times through the web console, with the laboratory parameter groups because
those are the ones that actually paint the shooting HUD.

A run passes when, at guest time >= 2.4 s:
  * GPU uploads >= 250 and rendered commands >= 200 (stock reaches ~300/250)
  * the frame has real content (decoded pixels are not all black)
  * cpu0 is not parked in the firmware's "no handler serviced this" spin

  python z62_webui/soak.py --repeats 10 --out .local/webui/soak.json
"""
import argparse
import hashlib
import json
import struct
import time
import urllib.request
import zlib

BASE = "http://127.0.0.1:8642"
LAB_AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                      for n, a in ((17, 0x132c0000), (18, 0x132e0000),
                                   (32, 0x13e80000)))
DEADLOOP = "101034fc"
MIN_GUEST = 2.4
MIN_UPLOADS = 250
MIN_RENDERED = 200
MIN_PIXELS = 50


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return r.read()


def get_json(path):
    return json.loads(get(path))


def post(path, payload=None):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload or {}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def png_stats(data):
    """Count non-black pixels in the console's filter-0 RGB PNG."""
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


def groups():
    """Bootable variants grouped by identical bytes."""
    seen, out = {}, []
    for entry in get_json("/api/variants")["variants"]:
        if entry["kind"] != "master":
            continue
        key = entry["sha256"]
        if key in seen:
            seen[key]["files"].append(entry["path"])
            continue
        item = dict(sha=key, files=[entry["path"]], label=entry.get("label"),
                    diff=entry.get("diff_bytes"))
        seen[key] = item
        out.append(item)
    return out


def one_run(variant_path, timeout=170):
    post("/api/stop")
    time.sleep(0.8)
    if not post("/api/variant", dict(path=variant_path)).get("ok"):
        return dict(pass_=False, error="adopt failed")
    inv = get_json("/api/state")["inputs"]
    paths = {k: inv[k]["path"] for k in ("master", "slave", "controller", "card")
             if inv[k].get("present")}
    started = post("/api/start", dict(paths, profile="z62-dual", aux=LAB_AUX))
    if not started.get("ok"):
        return dict(pass_=False, error=str(started))
    t0 = time.time()
    state = None
    while time.time() - t0 < timeout:
        time.sleep(4)
        state = get_json("/api/state")
        if not state["running"]:
            return dict(pass_=False, error="machine exited", guest=state.get("guest"))
        if state.get("guest") and state["guest"] >= MIN_GUEST:
            break
    pcs = {c["cpu"]: c["pc"] for c in get_json("/api/regs")["cores"]}
    post("/api/shot")
    png = get("/screen.png")
    width, height, nonblack = png_stats(png)
    post("/api/stop")
    uploads = (state.get("gpu") or {}).get("successful-uploads") or 0
    rendered = (state.get("gpu") or {}).get("rendered-commands") or 0
    cpu0 = pcs.get(0)
    ok = (uploads >= MIN_UPLOADS and rendered >= MIN_RENDERED
          and nonblack > MIN_PIXELS and cpu0 != DEADLOOP)
    return dict(pass_=ok, guest=state.get("guest"), uploads=uploads,
                rendered=rendered, nonblack=nonblack, cpu0=cpu0,
                png_sha=hashlib.sha256(png).hexdigest()[:12],
                png_bytes=len(png), wall=round(time.time() - t0, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--out", default=r"D:\z6ii_fw_lab\.local\webui\soak.json")
    ap.add_argument("--only", default="", help="run only variants matching this substring")
    args = ap.parse_args()

    report = dict(started=time.strftime("%Y-%m-%d %H:%M:%S"), repeats=args.repeats,
                  groups=[])
    selected = [g for g in groups()
                if not args.only or any(args.only in f for f in g["files"])]
    for group in selected:
        print("\n== %s  (%s)  diff=%s" % (group["files"][0], group["sha"],
                                          group["diff"]), flush=True)
        runs = []
        for i in range(args.repeats):
            result = one_run(group["files"][0])
            runs.append(result)
            print("  #%02d %-4s guest=%-5s up=%-4s rnd=%-4s px=%-5s cpu0=%s"
                  % (i + 1, "PASS" if result.get("pass_") else "FAIL",
                     result.get("guest"), result.get("uploads"),
                     result.get("rendered"), result.get("nonblack"),
                     result.get("cpu0")), flush=True)
        passed = sum(1 for r in runs if r.get("pass_"))
        group.update(runs=runs, passed=passed, failed=len(runs) - passed)
        report["groups"].append(group)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    print("\n== summary ==")
    for g in report["groups"]:
        print("%-34s %2d/%2d pass  diff=%s"
              % (g["files"][0], g["passed"], len(g["runs"]), g["diff"]))
    print("\nwrote", args.out)


if __name__ == "__main__":
    main()
