import os, re, sys, collections

ROOT = os.path.dirname(os.path.abspath(__file__))
MODS = []
for d in ("extracted", "all_modules"):
    for base, _, files in os.walk(os.path.join(ROOT, d)):
        for f in files:
            p = os.path.join(base, f)
            if os.path.getsize(p) > 400_000:
                MODS.append(p)

STR = re.compile(rb"[ -~]{5,}")
KEYS = ("SPI", "QSPI", "SPI0", "SPI1", "SPB", "NOR", "MXIC", "MX25", "W25N", "WINBOND",
        "W25Q", "GD25", "IS25", "N25Q", "MT25", "S25FL", "EN25", "P25Q", "Macronix",
        "FLASH", "FlashID", "SFDP", "eMMC", "MMC", "NAND", "OTP", "EFUSE", "EFuse",
        "SECURE", "SIGN", "VERIFY", "KEY", "BOOT", "PARTITION", "UPDATE")
hits = collections.defaultdict(list)
for p in MODS:
    blob = open(p, "rb").read()
    for m in STR.finditer(blob):
        s = m.group().decode()
        u = s.upper()
        if any(k.upper() in u for k in KEYS):
            hits[os.path.relpath(p, ROOT)].append((m.start(), s))

for p, lst in sorted(hits.items(), key=lambda kv: -len(kv[1])):
    print("\n== %s  (%d hits)" % (p, len(lst)))
    seen = set()
    for off, s in lst:
        if s in seen:
            continue
        seen.add(s)
        if len(seen) <= 45:
            print("   %08x  %s" % (off, s[:96]))
