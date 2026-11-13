"""Scan every probe subfolder for writes that could touch a wrong sector.

Flags raw-device paths, block-device names, dd/mtools, and open(...,'r+b')
style writes. Prints a per-folder summary; exit 1 if a forbidden pattern shows.

  python probes/audit_writes.py
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DANGER = [
    (re.compile(r"PHYSICALDRIVE|/dev/(sd|mmcblk|nvme|disk)", re.I), "块设备名"),
    (re.compile(r"\bdd\s+if=|\bdd\s+of=|\bmcopy\b|\bmtools\b", re.I), "dd/mtools"),
    (re.compile(r"open\([^)]*['\"][rwa]\+b['\"]"), "r+b 就地写"),
    (re.compile(r"\.seek\([^)]*\)\s*[\r\n]+\s*\w*\.write"), "seek+write"),
    (re.compile(r"win32file|CreateFile|DeviceIoControl", re.I), "Win32 设备 API"),
    (re.compile(r"0x0?4000000|NOR_|nor_off|flash_off", re.I), "NOR 偏移"),
]
WRITE = re.compile(r"open\([^)]*['\"](wb|w|ab)['\"]|write_bytes|write_text|\.write\(")

def main():
    bad = 0
    print(f"{'folder':22s} {'py':>3s} {'writes':>6s}  danger")
    for sub in sorted(p for p in HERE.iterdir() if p.is_dir()):
        files = list(sub.glob("*.py"))
        hits = []
        writes = 0
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            writes += len(WRITE.findall(text))
            for rx, label in DANGER:
                for m in rx.finditer(text):
                    line = text[:m.start()].count("\n") + 1
                    hits.append((f.name, line, label))
        bad += len(hits)
        desc = ", ".join(f"{n}:{l}({lab})" for n, l, lab in hits) or "-"
        print(f"{sub.name:22s} {len(files):3d} {writes:6d}  {desc}")
    print("\nOK" if bad == 0 else "\nFAIL: %d 处可疑写目标" % bad)
    return 0 if bad == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
