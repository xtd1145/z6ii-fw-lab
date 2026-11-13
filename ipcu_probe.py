#!/usr/bin/env python3
"""Full source inventory per core + inter-core IPC surface. Read-only."""
import re, sys, struct
from collections import Counter

M, S = "extracted/eg1850_mas_01700", "extracted/eg1850_sla_01700"
SRC = re.compile(rb"[A-Za-z][A-Za-z0-9_+.\-]{3,80}\.(?:c|cpp|h)\x00")
mas, sla = open(M, "rb").read(), open(S, "rb").read()


def srcs(b):
    return sorted({m.group(0)[:-1].decode() for m in SRC.finditer(b)})


print(f"--- mas source files ({len(srcs(mas))}) ---")
for f in srcs(mas):
    print("   " + f)
print(f"\n--- sla source files ({len(srcs(sla))}) ---")
for f in srcs(sla):
    print("   " + f)

TOK = [b"Ipcu", b"IPCU", b"Ipc", b"IPC_", b"Mbox", b"Mailbox", b"Slave", b"SLAVE",
       b"Master", b"MASTER", b"eg1850", b"EG1850", b"Intercore", b"Cores", b"Smp",
       b"Seq", b"JobId", b"TaskId", b"Queue", b"Fifo", b"Doorbell", b"Notify"]
print("\n--- inter-core / scheduling tokens (first 4 hits each, with neighbours) ---")
for name, img in (("mas", mas), ("sla", sla)):
    print(f" ## {name}")
    for t in TOK:
        offs = []
        st = 0
        while len(offs) < 4:
            i = img.find(t, st)
            if i < 0:
                break
            offs.append(i)
            st = i + 1
        if offs:
            print(f"   {t.decode():<10} n~{img.count(t):<6} @ " + " ".join(f"0x{o:X}" for o in offs))

STR = re.compile(rb"[ -~]{6,60}")
print("\n--- printable strings around Ipcu hits ---")
for name, img in (("mas", mas), ("sla", sla)):
    for m in re.finditer(rb"Ipcu", img):
        lo, hi = max(0, m.start() - 0x180), m.start() + 0x180
        got = [s.group(0).decode() for s in STR.finditer(img[lo:hi])]
        print(f" [{name} 0x{m.start():X}] " + " | ".join(got[:10]))

print("\n--- sla load address check: literals inside sla pointing past mas_end ---")
MAS_END = 0x10000000 + len(mas)
c = Counter()
mv = memoryview(sla).cast("I")
for x in mv:
    if MAS_END - (1 << 20) <= x < MAS_END + (4 << 20):
        c[x >> 16] += 1
for k, n in sorted(c.items()):
    print(f"   0x{k:05X}0000  x{n}   (offset from mas_end = {k<<16:>+,#})")
