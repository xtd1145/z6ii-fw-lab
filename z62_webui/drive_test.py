"""Drive the web console end to end over HTTP: state, NOR import, boot, keys."""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8642"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.read()


def post(path, payload=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        text = r.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main():
    post("/api/stop")
    time.sleep(1)
    state = json.loads(get("/api/state"))
    print("running:", state["running"], "profile:", state["profile"])
    for slot in ("master", "slave", "controller", "card"):
        info = state["inputs"][slot]
        print("  %-10s %-26s %s" % (slot, info.get("verdict"),
                                    (info.get("path") or "-").split("\\")[-1]))
    print("  parameters:", state["inputs"]["parameters"].get("source"))

    nor = post("/api/nor", dict(path=sys.argv[1]))
    print("\nNOR import: ok=%s groups=%s" %
          (nor.get("ok"), [(g["group"], g["accepted"]) for g in nor.get("groups", [])]))
    if not nor.get("ok"):
        return 1
    print("  aux:", nor["aux"][:110])

    state = json.loads(get("/api/state"))
    aux = state["inputs"]["parameters"].get("aux")
    print("inventory now uses:", state["inputs"]["parameters"].get("source"))

    paths = {s: state["inputs"][s]["path"]
             for s in ("master", "slave", "controller", "card")
             if state["inputs"][s].get("present")}
    cfg = dict(paths, profile="z62-dual", aux=aux)
    started = post("/api/start", cfg)
    print("\nstart:", "ok" if started.get("ok") else started)
    if not started.get("ok"):
        return 1

    t0 = time.time()
    for mark in (60, 120, 180):
        while time.time() - t0 < mark:
            time.sleep(1)
        post("/api/shot")
        st = json.loads(get("/api/state"))
        print("t~%3ds running=%s gpu=%s" % (mark, st["running"], st.get("gpu")))
        if mark == 120:
            print("  MENU:", post("/api/key", dict(key="menu", hold=5)))

    print("\nlog tail:\n", get("/api/log").decode()[-600:])
    post("/api/stop")
    print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
