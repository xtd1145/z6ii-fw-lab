#!/usr/bin/env python3
"""Timing-equivalence report for the Z62 model.

Why this exists
---------------
The model has no hardware delays. A register write is answered in zero
virtual time; silicon needs microseconds to tens of milliseconds depending on
what the write starts (PLL lock, flash erase, shutter travel, peer handshake).
Under `-icount shift=0` one guest instruction advances time by exactly 1 ns,
so every interval the guest can observe is the *instruction count*, not the
hardware response.

That gap cannot be closed by changing `shift`: raising it scales every
interval uniformly, while real hardware adds delay only on specific paths.
This tool therefore does not calibrate anything. It measures what the model
produces, compares it against the body measurements where those exist, and
prints the ratio - so "the timing matches" can never be claimed by accident.

Usage
-----
    python timing_probe.py --log .local/z62-smoke/board.txt
    python timing_probe.py --log board.txt --reference reference.json
    python timing_probe.py --run --at 55        # run QEMU first, then report

Reference format (fill in values measured on the body):

    {
      "t1_gap_us":       {"model_key": null, "body": [55.0, 23.0], "note": "..."},
      "t1_to_t2_ms":     {"model_key": "capseq", "body": [6.1]},
      "peer_handshake_ms": {"model_key": null, "body": [1.1]}
    }

`model_key` names an interval group this tool can measure from the log
(`capseq`, `shutter`, `gate`, `idmac`). `null` means the model cannot produce
that interval at all - which is itself the finding.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys

ROOT = r"D:\z6ii_fw_lab"

# Events that carry a virtual timestamp. Each pattern names the event and the
# fields this tool keeps.
EVENT_PATTERNS = {
    "capseq": re.compile(
        r"Z62_CAPSEQ event=(\S+).*?virtual_ns=(\d+)"),
    "shutter": re.compile(
        r"Z62_SHUTTER event=(\S+).*?virtual_ns=(\d+)"),
    "idmac": re.compile(
        r"Z62_IDMAC (kick|read) unit=(\d+)"),
}

# Body measurements already established from the hardware sessions. Kept here
# so a report always shows them next to the modelled value instead of letting
# the model number stand alone.
BODY_REFERENCE = {
    "t1_gap_us": {
        "body": [55.0, 23.0],
        "unit": "us",
        "note": "T1 interval, body. Model has no counterpart event: the "
                "capseq delay is a laboratory constant, not this interval.",
    },
    "t1_to_t2_ms": {
        "body": [6.1],
        "unit": "ms",
        "note": "T1 to T2 on the body.",
    },
    "peer_handshake_ms": {
        "body": [1.1],
        "unit": "ms",
        "note": "peer to handshake on the body.",
    },
}

# What the model's own constants claim, for side-by-side display.
MODEL_CONSTANTS = {
    "capseq-delay-ns": 200000,
    "shutter-erase-ns": 20000000,
    "shutter-program-ns": 100000,
    "frame-sync-period-ns": 33333333,
    "icount-shift-0-ns-per-insn": 1,
}

BODY_FILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "body_timing.json")


def load_body(path):
    """Return the measurement list, or [] if the file is missing/broken."""
    if not path or not os.path.exists(path):
        return []
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        print("  (body table unreadable: %s)" % exc)
        return []
    return doc.get("measurements", [])


def to_ns(value, unit):
    return int(round(value * (1000 if unit == "us" else 1000000)))


def emit_globals(measurements):
    """Print the -global / env fragment that applies the body measurements."""
    lines = []
    skipped = []
    for m in measurements:
        body = m.get("body_value")
        prop = m.get("qemu_property")
        if not body or not prop:
            skipped.append((m["id"], "no body value" if not body
                            else "no model knob"))
            continue
        ns = to_ns(body[0], m.get("unit", "us"))
        if m.get("env_not_property"):
            lines.append("set %s=%d" % (prop, ns))
        else:
            lines.append("-global %s=%d" % (prop, ns))
    return lines, skipped


def calibration_table(measurements):
    """Human-readable table: body value, model default, knob, status."""
    print("\n4. Calibration table (%s)" % os.path.basename(BODY_FILE_DEFAULT))
    print("-" * 68)
    print("  %-26s %-14s %-14s %s"
          % ("measurement", "body", "model default", "knob / status"))
    for m in measurements:
        body = m.get("body_value")
        unit = m.get("unit", "us")
        body_s = ("%.1f %s" % (body[0], unit)) if body else "-"
        default = m.get("model_default")
        default_s = ("%.3g ns" % default) if default is not None else "-"
        knob = m.get("qemu_property") or "(none - not modelled)"
        if knob.startswith("Z62_"):
            knob = knob + " (env)"
        status = m.get("status", "")
        flag = {"calibratable": "OK", "unmeasured": "TODO",
                "not_modelled": "N/A"}.get(status, status)
        print("  %-26s %-14s %-14s [%s] %s"
              % (m["id"][:26], body_s, default_s, flag, knob))
    print()
    todo = [m["id"] for m in measurements if m.get("status") == "unmeasured"]
    ok = [m["id"] for m in measurements if m.get("status") == "calibratable"]
    na = [m["id"] for m in measurements if m.get("status") == "not_modelled"]
    print("  calibratable (body value exists): %s" % (", ".join(ok) or "none"))
    print("  waiting for a body measurement : %s" % (", ".join(todo) or "none"))
    print("  no model event at all          : %s" % (", ".join(na) or "none"))


def parse_log(path):
    """Return {group: [(event, virtual_ns), ...]} in file order."""
    groups = {}
    if not os.path.exists(path):
        return groups
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            for group, pat in EVENT_PATTERNS.items():
                m = pat.search(line)
                if not m:
                    continue
                if group == "idmac":
                    # No timestamp in the IDMAC line; skip rather than guess.
                    continue
                event, ts = m.group(1), int(m.group(2))
                groups.setdefault(group, []).append((event, ts))
                break
    return groups


def intervals(events):
    """Gaps between consecutive timestamps, in ns, with their event labels."""
    out = []
    for (e1, t1), (e2, t2) in zip(events, events[1:]):
        if t2 >= t1:
            out.append((e1 + "->" + e2, t2 - t1))
    return out


def fmt_ns(ns):
    if ns >= 1e9:
        return "%.3f s" % (ns / 1e9)
    if ns >= 1e6:
        return "%.3f ms" % (ns / 1e6)
    if ns >= 1e3:
        return "%.3f us" % (ns / 1e3)
    return "%d ns" % ns


def report(log_path, ref_path=None, body_path=None):
    groups = parse_log(log_path)
    if not groups:
        print("no timestamped events in %s" % log_path)
        print("  (run with laboratory-capseq-trace=on / "
              "laboratory-shutter-hardware=on to produce them)")
        return 1

    print("=" * 68)
    print("Timing equivalence report")
    print("  log: %s" % log_path)
    print("=" * 68)

    print("\n1. Intervals the model produces")
    print("-" * 68)
    measured = {}
    for group, events in groups.items():
        gaps = intervals(events)
        if not gaps:
            print("  %-8s %d events, no interval (single event)" % (group, len(events)))
            continue
        print("  %-8s %d events, %d intervals" % (group, len(events), len(gaps)))
        by_kind = {}
        for label, ns in gaps:
            by_kind.setdefault(label, []).append(ns)
        for label, values in sorted(by_kind.items()):
            print("    %-32s n=%d  min=%s  med=%s  max=%s"
                  % (label, len(values),
                     fmt_ns(min(values)),
                     fmt_ns(statistics.median(values)),
                     fmt_ns(max(values))))
            measured[label] = values

    print("\n2. Model constants (laboratory values, NOT silicon)")
    print("-" * 68)
    for name, ns in MODEL_CONSTANTS.items():
        print("  %-30s %s" % (name, fmt_ns(ns)))

    print("\n3. Body reference vs model")
    print("-" * 68)

    reference = dict(BODY_REFERENCE)
    if ref_path and os.path.exists(ref_path):
        try:
            reference.update(json.load(open(ref_path)))
            print("  (merged %s)" % ref_path)
        except Exception as exc:
            print("  (reference file unreadable: %s)" % exc)

    for name, ref in reference.items():
        body = ref.get("body") or []
        print("  %-20s body: %s" % (name, ", ".join("%g %s" % (
            v, ref.get("unit", "")) for v in body) or "unknown"))
        key = ref.get("model_key")
        if key and key in measured:
            vals = measured[key]
            print("    %-18s model: %s" % ("", fmt_ns(statistics.median(vals))))
            if body:
                ratio = statistics.median(vals) / (body[0] * (1e3 if ref.get("unit") == "us" else 1e6))
                print("    %-18s ratio: model/body = %.4g" % ("", ratio))
        else:
            print("    %-18s model: NOT MEASURABLE (no counterpart event)"
                  % "")
        if ref.get("note"):
            print("    note: %s" % ref["note"])
        print()

    calibration_table(load_body(body_path or BODY_FILE_DEFAULT))

    print("\n5. Reading this report")
    print("-" * 68)
    print("  * A ratio far from 1 means the model cannot answer a question")
    print("    that depends on that interval (retry windows, timeouts,")
    print("    debounce, mechanical settling).")
    print("  * `NOT MEASURABLE` is a finding, not a gap in the tool: the")
    print("    model has no event that corresponds to that body interval.")
    print("  * Changing `-icount shift` scales every interval at once; it")
    print("    cannot make one path match silicon while leaving others alone.")
    print("  * Nothing here asserts hardware acceptance. Per the safety")
    print("    rules a model pass is `model_pass` only; hardware acceptance")
    print("    stays U until measured on the body.")
    print("  * To apply a body value: put it in body_timing.json and run")
    print("    `python timing_probe.py --emit` for the -global fragment.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.path.join(
        ROOT, r".local\z62-smoke\board.txt"))
    ap.add_argument("--reference", default="")
    ap.add_argument("--body", default=BODY_FILE_DEFAULT,
                    help="body measurement table (default body_timing.json)")
    ap.add_argument("--emit", action="store_true",
                    help="print the -global / set fragment for the body "
                         "measurements and exit")
    ap.add_argument("--run", action="store_true",
                    help="run QEMU first (needs board_probe.py next to this)")
    ap.add_argument("--at", default="60")
    args = ap.parse_args()

    if args.emit:
        measurements = load_body(args.body)
        if not measurements:
            print("no body table at %s" % args.body)
            return 1
        lines, skipped = emit_globals(measurements)
        print("# generated from %s" % args.body)
        print("# each line applies one body measurement to the matching knob")
        for line in lines:
            print(line)
        if skipped:
            print("#")
            print("# skipped (no body value or no knob):")
            for name, why in skipped:
                print("#   %-28s %s" % (name, why))
        return 0

    if args.run:
        probe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "board_probe.py")
        cmd = [sys.executable, probe, "--main-screen", "--at", args.at,
               "--out-dir", os.path.join(ROOT, r"work\qemu_build\run\screen\timing"),
               "--aux", ".local/parameters_real/group17.bin@0x132c0000,,"
                        ".local/parameters_real/group18.bin@0x132e0000,,"
                        ".local/parameters_real/group32.bin@0x13e80000",
               "--menu-start", "48", "--menu-every", "0",
               "--extra", "nikon-z62-lcd.live-view-fill=on,"
                          "nikon-z72-boot-config.laboratory-capseq-trace=on,"
                          "nikon-z72-boot-config.laboratory-capseq-actuator=on,"
                          "nikon-z72-boot-config.capseq-delay-ns=200000"]
        print("running: %s" % " ".join(cmd))
        subprocess.run(cmd)

    return report(args.log, args.reference)


if __name__ == "__main__":
    sys.exit(main())
