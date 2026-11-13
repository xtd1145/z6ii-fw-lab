import re, struct

MAS = r"D:\z6ii_fw_lab\all_modules\Z6_2_0170\eg1850_mas_01700"
b = open(MAS, "rb").read()

SIZES = [(6048, 4024), (4528, 3016), (3024, 2016), (4016, 4016), (3008, 3008),
         (2000, 2000), (6048, 3400), (4528, 2544), (3936, 2624), (2944, 1968),
         (1968, 1312), (3840, 2160), (1920, 1080), (8256, 6192), (4544, 3400)]

print("== where each (w,h) LE pair occurs")
loc = {}
for w, h in SIZES:
    p = struct.pack("<HH", w, h)
    hits = [m.start() for m in re.finditer(re.escape(p), b)]
    hits = [x for x in hits if x % 4 == 0]
    loc[(w, h)] = hits
    print("   %5d x %-5d  %d aligned hits  %s" % (w, h, len(hits),
                                                  ["0x%x" % x for x in hits[:6]]))

# cluster: find offsets where the FX and DX records sit within one table
anchor = loc.get((6048, 4024), [])
print("\n== table reconstruction around each FX anchor")
for a in anchor:
    blk = b[a - 0x40:a + 0xC0]
    recs = []
    for o in range(0, len(blk) - 8, 4):
        w, h = struct.unpack_from("<HH", blk, o)
        if 500 <= w <= 9000 and 500 <= h <= 9000 and w * h > 1e6:
            recs.append((a - 0x40 + o, w, h, struct.unpack_from("<H", blk, o + 4)[0],
                         struct.unpack_from("<H", blk, o + 6)[0]))
    if len(recs) >= 6:
        print("   anchor 0x%x -> %d records" % (a, len(recs)))
        for off, w, h, x, y in recs:
            print("      0x%06x  %5d x %-5d  f4=0x%04x f6=0x%04x" % (off, w, h, x, y))

print("\n== fps codes 24..120 as u16, co-located with a geometry pair?")
for w, h in ((3936, 2624), (6048, 4024), (1920, 1080), (3840, 2160)):
    for off in loc.get((w, h), []):
        win = struct.unpack_from("<16H", b, off - 0x20)
        fps = [v for v in win if v in (24, 25, 30, 50, 60, 100, 120, 45, 40, 15, 20)]
        if fps:
            print("   %d x %d @0x%x  nearby fps-like u16: %s" % (w, h, off, fps))
