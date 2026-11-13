# beep5 — boot-safe hook, no audio call (static study + build log)

Date: 2026-10-28. Tooling: `work/qemu_build/disrange.py` (`Z6II_MODE=thumb`),
`work/beep_fix/find_callers.py`, `work/beep_fix/make_variants.py` (encoders /
`reseal`), capstone 5.0.7. QEMU was **not** started (emulator busy).

Address convention: `audio_path_report.md` lists Thumb entry points (VA|1).
Real function starts are even; `find_callers.py` confirms this (e.g. 0 branches
to `0x1037281d`, 11 to `0x1037281c`). `disrange.py` labels its output addresses
`lo|1` while slicing from the even file offset, so every printed address there
is +1 relative to the true VA; all VAs below are normalised to even starts.

## Result

`dist/beep5/EG185070.BIN` is the **hook-only fallback**. It calls the displaced
routine and returns; **it adds no sound**. 22 bytes differ from stock.

Reason: of the four candidates, only `0x1037281c` has a calling convention that
can be established from evidence, and even that one cannot be *called* with
provable inputs — every candidate needs a runtime object/service state that no
static evidence in mas shows is live when the render-path hook fires.

## Evidence per candidate

### 1. `0x10242ee4` (report: `0x10242ee5`) — AUDIO_IF register programming
`find_callers.py 0x10242ee4` → 1 caller: `0x103728ea` (inside `0x1037281c`),
which passes `r0 = [stream_entry+0xc]`, `r1 = sp`.
Prologue `0x10242ee4`: `push.w {r4-r8,sb,sl,lr}; r4=r1; r5=r0; bl 0x1024347f`.
Body then does `ldr.w lr,[r5,#0x1c]` (0x10242f14) and `str.w r1,[lr,#0xc]`
(0x10242f30), `ldr r6,[r5,#0x1c]` (0x10242f46), `str r0,[r6,#0x10]` (0x10242f5e).
So `r0` is a per-stream driver object, `[obj+0x1c]` is the AUDIO_IF window, and
`r1` is the 0x1c-byte format block. No standalone contract: it is only ever
handed a field of the stream table. **Rejected.**

### 2. `0x10242fc0` (report: `0x10242fc1`) — TX FIFO drain
2 callers, both `mov r0,r4` then `bl`: `0x10243090` and `0x102435fa`.
Body reads the object at `r0`: `[+0x1c]` hw window (`ldr r4,[r0,#0x1c]`,
`ldr r2,[r4,#0x14]!` non-posted status poll, `strh r2,[r5],#2` sample store),
`[+0x28]` source samples, `[+0x34]` destination, `[+0x38]`/`[+0x3c]` counters.
Contract is `void f(struct dma_stream *)` — an object method with no way to
synthesize the object. **Rejected.**

### 3. `0x1037281c` (report: `0x1037281d`) — stream start / AUDIO_IF setup
11 callers (`0x1020b9c4, 0x1020ba96, 0x1036a0be, 0x1036a1ba, 0x1036a1d6,
0x103711cc, 0x1038536a, 0x10385838, 0x103be90c, 0x104843f4, 0x104bf6f6`).
Three call sites disassembled:
* `0x103711cc`: `r0 = [0xc1807b10]` (u8 stream id), `r1 = sp`, cfg built on the
  stack: `[sp]=1, [sp+1]=1, [sp+8..0xb]=0x004c4b40` (5 MHz), `[sp+0xc]=0x10`.
* `0x1038536a`: `r0 = [r4+3]`, `r1 = r4+0x88` (cfg inside the audio context).
* `0x1020b9c4`: `r0 = [r4+3]`, `r1 = r4+0x64`.

Convention (high confidence): `int f(u8 stream_index, u8 cfg[0x1c])`. Body:
`r4 = 0xc1808e28 + 104*index` (table `0xc1808e18+0x10`), copies cfg bytes into
a local 0x1c block, computes a divider from `[r4+0x50]`/`[r4+0x54]` and
`0x000186a0`, calls `0x10372e23(cfg)` (format selector `[cfg+5]` via `tbb`,
9 variants), `0x10372d8b(index,cfg,sp)`, then
`r0=[r4+0xc]; bl 0x10242ee4`.

Why it is still not callable:
* `0xc1808e18` is **outside the image** (file VA span `0x10020000..0x125147b8`),
  so the 104-byte stream entries are pure runtime DDR.
* The literal `0xc1808e18` occurs exactly twice in mas — the pools at
  `0x10372ae8` (inside `0x1037281c`) and `0x10372f08` (inside `0x10372d8b`).
  No initializer exists in this image, so nothing proves `[entry+0xc]` holds a
  driver object (with a valid `[obj+0x1c]`) at the moment `0x1073ecf8` runs.
  A zeroed entry would make `0x10242ee4` store through address `0x1c`.
* The required `cfg[5]` selector for a shutter tone is not derivable, and two
  different stream ids are used by two different contexts (see above).

Contract known, mandatory inputs and safety unknown → **do not call.**

### 4. `0x10302520` (report: `0x10302521`) — PCM player
`find_callers.py 0x10302520` → exactly **1** branch: `0x102dcdba`, a veneer
`b.w 0x10302520`. That veneer is reached only from veneer `0x102707f8`
(`b.w 0x102dcdba`), whose only feeder is `0x1039b6a2` (`b.w`). There is no call
site anywhere that sets up arguments, so the meaning of `r0` cannot be read off
a caller. From the body: guard `(r0+r1) > 0xa00 || r2 == 0 → return 0`
(0x10302524..0x1030253a); loop calls `0x1036a1a3(1)` (0x1030253e) →
`0x1036a1de(hdr, dst=r2, len)` (0x10302570) → `0x1036a1a3(0)` (0x10302580);
`hdr` is the 2-byte big-endian pair built from `r0`. `0x1036a1de` takes a lock
at `[0xc18a7800+4]` (`bl 0x10388159`) and drives `0x10372b6d` per 0xc0-byte
chunk. Both the `r0` semantics and the hw/lock preconditions are unproven.
**Rejected.**

## Decision

Fallback per instruction: hook-only, no extra call. It is byte-for-byte the
probeB shape (`make_variants.probes()`), which was already verified to boot.

## Build (`work/beep_fix/make_beep5.py`)

```
stub @ 0x1002d884 (16 bytes, stock cave verified zero 0x1002d884..0x1002d8c3)
  0x1002d884  2de90f50   push.w {r0, r1, r2, r3, ip, lr}
  0x1002d888  11f339f1   bl     0x1073eafe
  0x1002d88c  bde80f50   pop.w  {r0, r1, r2, r3, ip, lr}
  0x1002d890  11f334b2   b.w    0x1073ecfc

redirect @ 0x1073ecf8 (4 bytes, replaces `bl 0x1073eafe`, stock fff701ff)
  0x1073ecf8  eef4c4b5   b.w    0x1002d884
```

The redirect is a 32-bit **B.W, not BL** — that is what the shipped hook uses
(`make_variants.b(0x1002d884, 0x1073ecf8)` reproduces `eef4c4b5` bit-for-bit),
so the stub still enters with the caller's `lr`, exactly as in the verified
probeB build. Using `bl()` instead would encode `eef4c4f5` and change `lr` at
stub entry; it was rejected to stay identical to the known-safe shape.

No APSR/CPSR access anywhere (`mrs`/`msr`/`cps` absent from the stub); flags
after `pop.w` are untouched by the following `b.w`.

## Verification (all asserts passed)

Script asserts, plus an independent re-check in a second process:
* stub disassembles to exactly 4 instructions; branch targets decoded with
  `branch_decode` **and** capstone agree and are exact:
  `bl → 0x1073eafe`, `b.w → 0x1073ecfc`, redirect `b.w → 0x1002d884`.
  Nothing off by 2 or 4.
* targets cross-checked with `make_variants.read_targets()` on the shipped
  `dist/beep/EG185070.BIN` (original 0x1073eafe, return 0x1073ecfc) instead of
  trusting literals.
* `len(out) == 38750138`.
* diff vs `fw_verified/eg1850_mas_01700` = **22 bytes**: `0x00d884..0x00d893`
  (16), `0x71ecf8..0x71ecfb` (4), `0x24f47b8..0x24f47b9` (CRC). No other byte
  differs.
* CRC-16/HQX trailer: `reseal()` reproduces stock byte-for-byte and is
  idempotent on the new file (`stock 8e61 → beep5 8f39`).
* stub bytes are identical to the probeB shape assembled from
  `make_variants` (`2de90f50 11f339f1 bde80f50 11f334b2`).
* stock cave `0x1002d884..0x1002d8c3` is 64 zero bytes, so nothing in stock is
  overwritten by the stub.

## Caveats

* Static only: no boot executed for this build (QEMU in use by another
  process). "Cannot fault" rests on shape equivalence with probeB plus the
  byte-diff proof, not on a run.
* Only `dist/beep5/` was created; no other `dist/` content, `nikon-z72.c` or
  QEMU sources were touched.
* `dist/probeB/` no longer exists in the tree; equivalence was checked by
  re-assembling the probeB definition in memory.
