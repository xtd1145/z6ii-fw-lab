import os, re, collections

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = [
    "extracted/eg1850_mas_01700", "extracted/eg1850_sla_01700",
    "extracted/ex1850_012000.bi", "extracted/vr1850_010100.bi",
    "extracted/_tpj01_v140.bin",
]
TARGETS = [os.path.join(ROOT, t) for t in TARGETS if os.path.exists(os.path.join(ROOT, t))]
print("targets:", [os.path.basename(t) for t in TARGETS])

STR = re.compile(rb"[ -~]{4,}")
GROUPS = {
    "nor/spi":  r"(?i)(serial_?nor|spinor|spi_?nor|spiflash|spi0|spi1|spb|qspi|sfdp|flashid|jedec|erase|sector|block_?protect|^nor)",
    "part":     r"(?i)(partition|bootloader|boot_?block|fw_?area|backup|redundan|A_?side|B_?side|update_?bs|romfile|noraddr|flashaddr)",
    "size":     r"(?i)(0x[0-9a-f]{6,8}byte|NOR_?SIZE|FLASH_?SIZE|MAX_?ADDR|CAPACITY)",
    "calib":    r"(?i)(calibration|af_?adj|lens_?adj|shading|prsn|serial_?number|bodyid|uuid|product_?id|customer_?setting|whitebal|dsn|w.b.|geomet)",
    "crypto":   r"(?i)(sha1|sha256|aes|rsa|ecdsa|cmac|hmac|otp|efuse|key_?slot|rootkey|publickey|sigcheck|verifysig|signature)",
    "boot":     r"(?i)(bootrom|boot_?stl|resetvector|vector_?table|stage1|loader|itron|edk|secentry)",
}
for p in TARGETS:
    blob = open(p, "rb").read()
    print("\n===== %s  (%d bytes)" % (os.path.basename(p), len(blob)))
    for name, pat in GROUPS.items():
        rx = re.compile(pat)
        found = []
        seen = set()
        for m in STR.finditer(blob):
            s = m.group().decode()
            if rx.search(s) and s not in seen:
                seen.add(s)
                found.append((m.start(), s))
        print("  -- %s: %d unique" % (name, len(found)))
        for off, s in found[:30]:
            print("       %08x  %s" % (off, s[:88]))
