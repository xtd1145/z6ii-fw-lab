"""Boot the stock Z6 II pair N times and judge each boot.

Same pass criteria as z62_webui/soak.py, but always uses the currently selected
stock inputs instead of switching dist/ variants:
  guest >= 2.4 s, GPU uploads >= 250, rendered >= 200, non-black pixels > 30,
  cpu0 is not in the firmware's no-handler spin. The clean HUD (no Error box)
  samples ~37 non-black pixels, so the old >50 threshold only fit error-screen
  frames.

  python work/soak_stock.py --repeats 100
"""
import argparse
import atexit
import ctypes
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
import zlib

BASE = "http://127.0.0.1:8642"
LOCK = r"D:\z6ii_fw_lab\.local\webui\soak_stock.lock"


def _alive(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def acquire_lock():
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip())
        except ValueError:
            pid = 0
        if pid and _alive(pid):
            sys.exit("another soak is already running (pid %d)" % pid)
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))

    def release():
        try:
            os.remove(LOCK)
        except OSError:
            pass

    atexit.register(release)
LAB_AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                      for n, a in ((17, 0x132c0000), (18, 0x132e0000),
                                   (32, 0x13e80000)))
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
        except Exception as exc:  # the console drops a connection mid restart
            last = exc
            time.sleep(2)
    raise last


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read()


def get_json(path):
    return json.loads(_retry(get, path))


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


def one_run(timeout=200):
    post("/api/stop")
    time.sleep(1.0)
    inv = get_json("/api/state")["inputs"]
    paths = {k: inv[k]["path"] for k in ("master", "slave", "controller", "card")
             if inv[k].get("present")}
    started = post("/api/start", dict(paths, profile="z62-dual", aux=LAB_AUX))
    if not started.get("ok") and "already running" not in str(started.get("error", "")):
        return dict(pass_=False, error=str(started))
    t0 = time.time()
    state = None
    while time.time() - t0 < timeout:
        time.sleep(4)
        state = get_json("/api/state")
        if not state["running"]:
            return dict(pass_=False, error="machine exited",
                        guest=state.get("guest"),
                        wall=round(time.time() - t0, 1))
        if state.get("guest") and state["guest"] >= MIN_GUEST:
            break
    pcs = {c["cpu"]: c["pc"] for c in get_json("/api/regs")["cores"]}
    post("/api/shot")
    png = _retry(get, "/screen.png")
    width, height, nonblack = png_stats(png)
    post("/api/stop")
    uploads = (state.get("gpu") or {}).get("successful-uploads") or 0
    rendered = (state.get("gpu") or {}).get("rendered-commands") or 0
    cpu0 = pcs.get(0)
    ok = (state.get("guest") is not None and state["guest"] >= MIN_GUEST
          and uploads >= MIN_UPLOADS and rendered >= MIN_RENDERED
          and nonblack > MIN_PIXELS and cpu0 != DEADLOOP)
    return dict(pass_=ok, guest=state.get("guest"), uploads=uploads,
                rendered=rendered, nonblack=nonblack, cpu0=cpu0,
                png_sha=hashlib.sha256(png).hexdigest()[:12],
                png_bytes=len(png), wall=round(time.time() - t0, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=100)
    ap.add_argument("--out", default=r"D:\z6ii_fw_lab\.local\webui\soak100.json")
    args = ap.parse_args()
    acquire_lock()
    report = dict(started=time.strftime("%Y-%m-%d %H:%M:%S"),
                  repeats=args.repeats, runs=[])
    for i in range(args.repeats):
        result = one_run()
        result["n"] = i + 1
        report["runs"].append(result)
        passed = sum(1 for r in report["runs"] if r.get("pass_"))
        print("#%03d %-4s guest=%-5s up=%-4s rnd=%-4s px=%-5s cpu0=%-8s %.1fs"
              % (i + 1, "PASS" if result.get("pass_") else "FAIL",
                 result.get("guest"), result.get("uploads"),
                 result.get("rendered"), result.get("nonblack"),
                 result.get("cpu0"), result.get("wall") or 0),
              flush=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    passed = sum(1 for r in report["runs"] if r.get("pass_"))
    print("== summary %d/%d pass" % (passed, len(report["runs"])))
    print("wrote", args.out)


if __name__ == "__main__":
    main()
