"""Regression gate: the release handshake must be sensitive to the ack value.

This is the test v9 did not have. It runs the guest twice and asserts the two
outcomes are different:

  strict  (default)      -> foreground comes up, cpu0 not parked in the poll
  --starve (body silent) -> cpu0 parked at 0x10200e60, no GPU uploads

If both runs look the same, the model has lost its sensitivity and a wrong
ack value would once again pass silently - which is exactly how the v9 brick
shipped to hardware.

  python gate_regression.py
  python gate_regression.py --at 30 --verbose
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "gate_probe.py")


def run_probe(py, at, starve, verbose):
    args = [py, PROBE, "--at", str(at), "--port-base",
            "4600" if starve else "4580"]
    if starve:
        args.append("--starve")
    proc = subprocess.run(args, capture_output=True, text=True, cwd=HERE,
                          timeout=600)
    out = proc.stdout + proc.stderr
    if verbose:
        print(out)
    # gate_probe prints "★ 停在 ..." for a hit and "未停在 ..." otherwise, so
    # match the star marker - a bare substring test would match the negative
    # message too.
    parked = "★ 停在 release 轮询循环" in out
    uploads = None
    m = re.search(r"gpu successful-uploads\s*=\s*(\d+)", out)
    if m:
        uploads = int(m.group(1))
    pc0 = None
    m = re.search(r"cpu0=0x([0-9a-f]{8})", out)
    if m:
        pc0 = int(m.group(1), 16)
    return {"parked": parked, "uploads": uploads, "pc0": pc0, "raw": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=40.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    py = sys.executable

    print("=== 1/2 严格模式（正常机身）===")
    strict = run_probe(py, args.at, False, args.verbose)
    print("  cpu0=%s  parked=%s  uploads=%s"
          % ("0x%08x" % strict["pc0"] if strict["pc0"] else "?",
             strict["parked"], strict["uploads"]))

    print("=== 2/2 starve 模式（硬件不应答）===")
    starve = run_probe(py, args.at, True, args.verbose)
    print("  cpu0=%s  parked=%s  uploads=%s"
          % ("0x%08x" % starve["pc0"] if starve["pc0"] else "?",
             starve["parked"], starve["uploads"]))

    ok = True
    print("\n=== 判定 ===")
    if strict["parked"]:
        print("FAIL 严格模式下不应停在轮询循环"); ok = False
    else:
        print("PASS 严格模式未停轮询")
    if strict["uploads"] is None or strict["uploads"] <= 0:
        print("FAIL 严格模式下取景未起来（uploads=%s）" % strict["uploads"])
        ok = False
    else:
        print("PASS 严格模式取景正常（uploads=%d）" % strict["uploads"])

    if not starve["parked"]:
        print("FAIL starve 模式未停在轮询循环 —— 模型丢失了对 ack 的敏感性，"
              "v9 类错误会再次静默通过")
        ok = False
    else:
        print("PASS starve 模式停在 release 轮询（v9 黑砖可复现）")
    if starve["uploads"] not in (None, 0):
        print("FAIL starve 模式不应有取景上载（uploads=%s）" % starve["uploads"])
        ok = False
    else:
        print("PASS starve 模式无取景上载")

    if strict["pc0"] == starve["pc0"] and strict["pc0"] is not None:
        print("FAIL 两态 PC 相同，模型对 ack 不敏感"); ok = False

    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
