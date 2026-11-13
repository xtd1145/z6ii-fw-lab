"""Run every bootable patch under the status-clear scaffold and judge the UI.

The Error box used to cover the shooting screen in every run, which made it
impossible to tell whether a patch broke the UI. With Z62_CLEAR_STATUS=1 the
status list is cleared at guest 1.3 s, the HUD renders clean, and MENU opens, so
the same measurement now says something about the patch itself.

Per variant: adopt -> start (lab params, clear_status) -> wait until guest 2.4 s
-> capture the HUD -> press MENU -> capture again. A patch passes when the HUD
has content, cpu0 is not in the firmware's deadloop, and MENU produces a
different frame.

  python work/beep_fix/patch_matrix.py --runs 2
"""
import argparse
import hashlib
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8642"
LAB_AUX = ",,".join(".local/parameters_lab/group%d.bin@%#x" % (n, a)
                      for n, a in ((17, 0x132c0000), (18, 0x132e0000),
                                   (32, 0x13e80000)))
SKIP = ("dist/statuswriter", "dist/copyprobe", "dist/installprobe",
        "dist/installprobe2", "dist/bulkprobe", "dist/doorbell",
        "dist/srcprobe", "dist/beep/", "dist/noop/", "dist/probe/",
        "dist/beep4/")


def get(path, raw=False):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        data = r.read()
    return data if raw else json.loads(data)


def post(path, payload=None):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload or {}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def h(data):
    return hashlib.sha256(data).hexdigest()[:8]


def one(variant, timeout=200):
    post("/api/stop")
    time.sleep(0.8)
    info = post("/api/variant", dict(path=variant))
    if not info.get("ok"):
        return dict(ok=False, error=info.get("error"))
    inv = get("/api/state")["inputs"]
    paths = {k: inv[k]["path"] for k in ("master", "slave", "controller", "card")
             if inv[k].get("present")}
    started = post("/api/start", dict(paths, profile="z62-dual", aux=LAB_AUX,
                                      clear_status=True))
    if not started.get("ok"):
        return dict(ok=False, error=str(started))
    t0 = time.time()
    state = None
    while time.time() - t0 < timeout:
        time.sleep(5)
        state = get("/api/state")
        if not state["running"]:
            return dict(ok=False, error="exited", guest=state.get("guest"))
        if state.get("guest") and state["guest"] >= 2.4:
            break
    cpu0 = {c["cpu"]: c["pc"] for c in get("/api/regs")["cores"]}.get(0)
    post("/api/shot")
    hud = get("/screen.png", raw=True)
    post("/api/key", dict(key="menu", hold=2.5))
    time.sleep(14)
    post("/api/shot")
    menu = get("/screen.png", raw=True)
    post("/api/stop")
    uploads = (state.get("gpu") or {}).get("successful-uploads") or 0
    return dict(ok=True, guest=state.get("guest"), uploads=uploads, cpu0=cpu0,
                hud_bytes=len(hud), hud=h(hud), menu_bytes=len(menu), menu=h(menu),
                menu_changed=h(hud) != h(menu))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--out", default=r"D:\z6ii_fw_lab\.local\webui\patch_matrix.json")
    args = ap.parse_args()

    variants = [v["path"] for v in get("/api/variants")["variants"]
                if v["kind"] == "master"
                and not any(v["path"].startswith(s) for s in SKIP)]
    report = dict(started=time.strftime("%Y-%m-%d %H:%M"), runs=args.runs,
                  variants=[])
    for path in sorted(variants):
        entry = dict(path=path, results=[])
        for i in range(args.runs):
            r = one(path)
            entry["results"].append(r)
            print("%-34s run%d ok=%-5s guest=%-5s up=%-4s cpu0=%s hud=%s(%d) "
                  "menu=%s changed=%s" %
                  (path.split("dist/")[-1], i + 1, r.get("ok"), r.get("guest"),
                   r.get("uploads"), r.get("cpu0"), r.get("hud"),
                   r.get("hud_bytes", 0), r.get("menu"), r.get("menu_changed")),
                  flush=True)
        report["variants"].append(entry)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    print("\n== summary ==")
    for v in report["variants"]:
        good = sum(1 for r in v["results"] if r.get("menu_changed"))
        print("%-34s %d/%d 菜单可用" % (v["path"].split("dist/")[-1], good,
                                      len(v["results"])))
    print("wrote", args.out)


if __name__ == "__main__":
    main()
