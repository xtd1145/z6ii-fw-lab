import sys, struct, hashlib, re

CTX = 0xC2B2B608
FN_TRANSFORM = 0x1039EA3D
PARAM = 0x44C
MODEL = b"Z_6_2"
SEED = bytes.fromhex("0023002400250026")


def load_container(path):
    d = open(path, "rb").read()
    digest = d[0x00:0x14]
    body = d[0x20:]
    return digest, body


def check(digest, body, k8, label):
    got = hashlib.sha1(body + k8).digest()
    ok = got == digest
    print(f"  [{label}] K8={k8.hex()}  SHA1(body||K8)={got.hex()}  {'MATCH' if ok else 'no'}")
    return ok


def find_ctx(buf, base_hint=None):
    """Locate the 0x34-byte verifier context by structural anchors."""
    out = []
    for m in re.finditer(re.escape(struct.pack("<I", FN_TRANSFORM)), buf):
        off = m.start() - 8
        if off < 0 or off + 0x34 > len(buf):
            continue
        p = struct.unpack_from("<I", buf, off + 0x14)[0]
        if p != PARAM:
            continue
        out.append(off)
    if not out:
        for m in re.finditer(re.escape(MODEL), buf):
            out.append(m.start() - 0x18)
    return [o for o in out if o >= 0]


def report(buf, digest, body, dump_base=None):
    hits = find_ctx(buf)
    if not hits:
        print("no ctx anchor found; trying raw 8-byte candidates near 'Z_6_2'")
    for o in hits:
        v = o + (dump_base if dump_base is not None else 0)
        ptr, ln, fn = struct.unpack_from("<III", buf, o)
        key = buf[o + 0x0C:o + 0x14]
        model = buf[o + 0x18:o + 0x20].split(b"\0")[0]
        exp = buf[o + 0x20:o + 0x34]
        print(f"ctx@0x{v:08x} data=0x{ptr:08x} len=0x{ln:x} fn=0x{fn:08x} "
              f"model={model.decode(errors='replace')} expect={exp.hex()}")
        print(f"  slot[0x0c] = {key.hex()}")
        if ln == len(body):
            print("  len matches container body -> this IS the container verify context")
        check(digest, body, key, "dump+0x0c")
        if key != SEED:
            print("  note: slot differs from static seed -> transform already ran")


def parse_memdump_text(path):
    """Accept either byte columns (aa bb cc) or 32-bit word columns (c1887bc0).
    Lines with >=3 8-hex tokens are treated as word columns."""
    out = bytearray()
    kinds = {"byte": 0, "word": 0}
    for line in open(path, "r", errors="replace"):
        toks = line.replace(":", " ").split()
        if len(toks) >= 5 and re.fullmatch(r"[0-9a-fA-F]{4,10}", toks[0]):
            toks = toks[1:]
        line = " ".join(toks)
        words = re.findall(r"\b[0-9a-fA-F]{8}\b", line)
        pairs = re.findall(r"\b[0-9a-fA-F]{2}\b", line)
        if len(words) >= 3:
            for w in words:
                out += struct.pack("<I", int(w, 16))
            kinds["word"] += 1
        elif len(pairs) >= 4:
            out += bytes(int(h, 16) for h in pairs)
            kinds["byte"] += 1
    print(f"parsed {len(out)} bytes ({kinds['byte']} byte-lines, {kinds['word']} word-lines)")
    return bytes(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage:\n  python {sys.argv[1]} <raw-dump.bin> [container.dec]\n"
              f"  python {sys.argv[1]} --text <memdump.log> [container.dec]\n"
              f"  python {sys.argv[1]} --k8 <8字节的hex> [container.dec]\n"
              f"  python {sys.argv[1]} --selftest")
        sys.exit(0)

    dec = "Z6_2_0170.dec"
    if sys.argv[1] == "--selftest":
        buf = bytearray(0x2000)
        o = 0x100
        struct.pack_into("<III", buf, o, 0x10000000 + 0x20, 0x2EBB968, FN_TRANSFORM)
        buf[o + 0x0C:o + 0x14] = SEED
        struct.pack_into("<I", buf, o + 0x14, PARAM)
        buf[o + 0x18:o + 0x1F] = MODEL
        print("selftest ctx offsets found:", [hex(x) for x in find_ctx(bytes(buf))])
        sys.exit(0)

    mode = sys.argv[1]
    if mode == "--k8":
        digest, body = load_container(dec if len(sys.argv) < 4 else sys.argv[3])
        check(digest, body, bytes.fromhex(sys.argv[2]), "cli")
    elif mode == "--text":
        digest, body = load_container(sys.argv[3] if len(sys.argv) > 3 else dec)
        report(parse_memdump_text(sys.argv[2]), digest, body, CTX)
    else:
        digest, body = load_container(sys.argv[2] if len(sys.argv) > 2 else dec)
        report(open(mode, "rb").read(), digest, body)
