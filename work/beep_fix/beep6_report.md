# beep6 / noop6 — APSR pair deleted, BeepPlay dropped (static rebuild)

Date: 2026-10-28. Tooling: capstone 5.0.7, `work/beep_fix/make_variants.py`
(encoders, `reseal`), new builder `work/beep_fix/make_beep6.py`. QEMU was **not**
started; the emulator is in use by another process.

## 1. Result

| file | size | SHA-256 |
|---|---|---|
| `dist/beep6/EG185070.BIN` | 38750138 | `fa494a68e2b59f04e3bb98a59554fa8c946a8d95e2376133ca0ef216f9728377` |
| `dist/noop6/EG185070.BIN` | 38750138 | `fa494a68e2b59f04e3bb98a59554fa8c946a8d95e2376133ca0ef216f9728377` |

* Both are byte-identical to each other, because **the BeepPlay call was
  dropped** (§5). `beep6` therefore adds no sound; it is the boot-safe hook.
* **30 bytes differ** from `fw_verified/eg1850_mas_01700`: the 24-byte stub,
  the 4-byte redirect, and the 2-byte CRC trailer. Nothing else.
* Every branch in the stub decodes to its exact target: `bl 0x1073eafe`,
  `b.w 0x1073ecfc`; the redirect decodes to `0x1002d884`.

## 2. What was changed, byte for byte

Source bytes: `dist/EG185070_beep.BIN` (SHA-256 `6340c17e…`, byte-identical to
the top-level `dist/EG185070.BIN` and to `dist/beep/EG185070.BIN`) and
`dist/EG185070_noop.BIN` (`4c2682b7…`, identical to `dist/poc/extracted_mas.BIN`).
Both carry the same 4-byte redirect `eef4c4b5` at `0x1073ecf8`.

Shipped beep stub @ `0x1002d884` (38 bytes, `0x1002d884..0x1002d8a9`):

```
0x1002d884  2de90f50   push.w {r0, r1, r2, r3, ip, lr}
0x1002d888  eff30084   mrs    r4, apsr                  <- deleted (a)
0x1002d88c  30b4       push   {r4, r5}                 kept
0x1002d88e  82b0       sub    sp, #8                   kept
0x1002d890  6a46       mov    r2, sp                   <- BeepPlay out-param setup, dropped with (c)
0x1002d892  11f334f1   bl     0x1073eafe               kept (b)
0x1002d896  3bf36af1   bl     0x10768b6e               <- BeepPlay, dropped (c)
0x1002d89a  02b0       add    sp, #8                   kept
0x1002d89c  30bc       pop    {r4, r5}                 kept
0x1002d89e  84f3008c   msr    apsr_nzcvqg, r4           <- deleted (a)
0x1002d8a2  bde80f50   pop.w  {r0, r1, r2, r3, ip, lr}
0x1002d8a6  11f329b2   b.w    0x1073ecfc
```

The shipped noop stub is the same minus the BeepPlay `bl` and 4 bytes shorter
(`msr` at `0x1002d89a`, `b.w` at `0x1002d8a2` = `11f32bb2`).

Deleted in both builds:

* (a) `mrs r4, apsr` + `msr apsr_nzcvqg, r4` — the pair the bisect pins as fatal.
* (c) `bl 0x10768b6e` **and** its `mov r2, sp`: the `mov` exists only to give
  BeepPlay a writable out-parameter (BeepPlay does `str r5, [r2]` at
  `0x10768b78`). With the call gone it has no consumer. It is provably
  harmless either way: the displaced routine never reads `r2` — its first
  touch of `r2` is `add r2, sp, #8` at `0x1073eb1a`, i.e. a write before any
  read (`0x1073eafe..0x1073eb1a` only reads `r0`/`r1`).

Kept: the whole stack frame. `push.w {r0-r3,ip,lr}` (24) + `push {r4,r5}` (8) +
`sub sp,#8` (8) = 40 bytes = 5x8, so SP stays 8-byte aligned at the displaced
`bl`, exactly as shipped. `push/pop {r4,r5}` is not cosmetic: the continuation
at `0x1073ecfc` uses `r4` (`movs r7,#1` / `str r5,[r4,#0x3c]`), so the stub
must hand the caller's `r4`/`r5` back. It does.

## 3. New stub (identical in beep6 and noop6)

```
0x1002d884  2de90f50   push.w {r0, r1, r2, r3, ip, lr}
0x1002d888  30b4       push   {r4, r5}
0x1002d88a  82b0       sub    sp, #8
0x1002d88c  11f337f1   bl     0x1073eafe        -> 0x1073eafe
0x1002d890  02b0       add    sp, #8
0x1002d892  30bc       pop    {r4, r5}
0x1002d894  bde80f50   pop.w  {r0, r1, r2, r3, ip, lr}
0x1002d898  11f330b2   b.w    0x1073ecfc        -> 0x1073ecfc
```

Redirect @ `0x1073ecf8` (unchanged from both shipped patches, 4 bytes):

```
0x1073ecf8  eef4c4b5   b.w 0x1002d884
```

Stub bytes: `2de90f50 30b4 82b0 11f337f1 02b0 30bc bde80f50 11f330b2`.
No `mrs`/`msr`/`cps`/`svc`/`it` and no APSR/CPSR reference anywhere in it.
Entry contract is unchanged from the verified probeB/beep5 shape: the redirect
is a **B.W** (not BL), so the stub is entered with the caller's `lr`; `pop.w`
restores it before `b.w` jumps to `0x1073ecfc`.

## 4. Static verification (asserts in `make_beep6.py`, re-run independently
from disk in a second process)

1. **Length** `== 38750138` for both files.
2. **Branch targets decode exactly.** Decoded twice — with
   `make_variants.branch_decode()` and with capstone's own (detail) operand —
   and both agree, exact, not off by 2 or 4:
   * stub `bl` @ `0x1002d88c` -> `0x1073eafe`
   * stub `b.w` @ `0x1002d898` -> `0x1073ecfc`
   * redirect `b.w` @ `0x1073ecf8` -> `0x1002d884`
   * no other branch exists in the stub (no call is kept; `bl 0x10768b6e` is absent)
   The displaced instruction is still stock `fff701ff` = `bl 0x1073eafe` @
   `0x1073ecf8`, and the continuation at `0x1073ecfc` is stock
   `0127e563c4e91357`.
   The stub disassembles to exactly 8 instructions covering all 24 bytes
   (`b"".join(bytes) == stub`), no `.byte`.
3. **Diff vs `fw_verified/eg1850_mas_01700` = 30 bytes**, all inside the
   allowed set:
   ```
   0x00d884..0x00d89b  VA 0x1002d884..0x1002d89b   24  stub
   0x71ecf8..0x71ecfb  VA 0x1073ecf8..0x1073ecfb    4  redirect
   0x24f47b8..0x24f47b9 VA 0x125147b8..0x125147b9   2  CRC trailer
   ```
   The rest of the image is byte-identical to stock.
4. **CRC** `binascii.crc_hqx(data[:-2], 0)` stored big-endian reproduces the
   trailer: stock `8e61` (itself reproduced), beep6/noop6 `e921`.
   `reseal()` is idempotent on both new files and reproduces stock exactly.
5. The target cave is untouched zero fill in stock
   (`0x1002d884..0x1002d8ff` all zero), so the stub overwrites nothing; bytes
   `0x1002d89c..0x1002d8c3` remain zero after the shorter stub.

## 5. BeepPlay (`0x10768b6e`) — verdict: **it blocks. Dropped.**

`work/audio_path_report.md`/`work/loc/sb_path.md` describe this routine at
"`0x10748b6e`", i.e. with a `0x10000000` image base. With the real base
(`0x10020000`, file offset = VA - `0x10020000`) the same code is at
**`0x10768b6e`**, which is exactly the stub's `bl` target, and the strings
`0x10768bc8` "BeepPlay failed." / `0x10768bdc` "BeepPlay was timeout." sit
inside it. Disassembly:

```
0x10768b6e  push {r3, r4, r5, r6, r7, lr}
0x10768b70  movs r6, #3                 ; AO slot = 3
0x10768b72  mov  r4, r2                 ; r2 = caller's out-param
0x10768b78  str  r5, [r2]               ; *out = 0        <- r2 must be writable
0x10768b7a  bl   0x10524004             ; AO_Begin(3)
0x10768b82  bl   0x10376c32             ; async send (returns immediately)
0x10768b86  cbz  r0, 0x10768bb0         ; send failed -> *out = 1, return 3
0x10768b88  movw r2, #0x1388            ; timeout = 5000 ms   <-- WAIT
0x10768b8c  mov  r1, sp                 ; reply buffer = own stack
0x10768b8e  mov  r0, r6                 ; slot 3
0x10768b90  bl   0x10524016             ; AO_Wait(3, sp, 5000) <-- WAIT
0x10768b94  cbz  r0, 0x10768bb8         ; 0 => timeout -> *out = 3, log, return 3
```

The success path after `0x10768b86` is unconditional: the only branch is the
send-failure test, so every successful send reaches the `movw/mov/bl` triple at
`0x10768b88..0x10768b90`. That call cannot be skipped.

What the wait does:

* `0x10524016` is a thunk: `r3 = r2` (timeout), `r2 = r1` (reply buffer),
  `r1 = r0` (slot), `r0 = [0x1052402c] = 0xc1819b1c` (AO manager), then
  `b.w 0x1035d3be`.
* `0x1035d3be` pushes `{r2,r3,r4,lr}`, puts **timeout at `[sp]`, 0 at `[sp+4]`**
  (`strd r3, r4, [sp]` @ `0x1035d3c2`), dereferences the manager
  (`ldr r0,[r0]` @ `0x1035d3ca`), sets `r2 = 1`, and tail-calls the primitive
  `0x10104a14` (`blx` @ `0x1035d3d0`) with `r3` = reply buffer.
  It returns `r0 = 1` only when the primitive returns 0, else `r0 = 0`
  (the `cbz`/`movs r0,#0` / `movs r0,#1` pair at `0x1035d3d4..0x1035d3dc`),
  and BeepPlay treats `0` as timeout.
* `0x10104a14` is **A32** (not Thumb) and is a system-call veneer:
  ```
  0x10104a14  stmdb sp!, {r4}
  0x10104a18  add   r4, sp, #4
  0x10104a1c  stmdb sp!, {lr}
  0x10104a20  ldr   ip, [pc, #0xc]     ; [0x10104a34] = 0x80300500  (op id)
  0x10104a24  svc   #6
  0x10104a28  ldm   sp!, {lr}
  0x10104a2c  ldm   sp!, {r4}
  0x10104a30  bx    lr
  ```
  So the wait is a trap into the monitor with op `0x80300500`; the calling
  thread does not continue past `svc #6` until the monitor returns. (57 such
  `svc #6` veneers exist in the image; the same primitive is used elsewhere
  with timeout `-1`, e.g. `mov.w r3,#-1` at `0x1035d386`/`0x1035d3a4`.)

On **what** it waits: the completion event of the request posted by
`0x10376c32`. That routine allocates a 12-byte + 8-byte block, stores the
payload byte and the master-side descriptor `0x10376633`, and calls
`0x102b4448` with msgid `0x0419000d + 0x26 = 0x04190033` (`0x10376c62`,
`0x10376c64`, literal at `0x10376cf0`). `0x102b4448` allocates a 0x1c-byte
message block, stores the dispatch trampoline `0x102b442f` and the msgid
(`0x102b445a..0x102b4466`), posts it to the queue object at `[0xc186458c]`
via `0x103d651c`, and returns 1 (`0x102b446c`). That is a **queue post, not an
inline dispatch**: the reply has to be produced later by whichever context
drains that queue, and it is delivered through the descriptor `0x10768b55`
(loaded at `0x10768b7e`).

Consequences, all static:

* BeepPlay is a synchronous request/reply wrapper. After a successful send it
  cannot return until another execution context services `0x04190033` and
  signals slot 3, or until 5000 ms expire.
* The constant `0x04190033` occurs **0 times** in the whole 38,750,138-byte
  image (only its base `0x0419000d` occurs once, at `0x10376cf0`), and
  `work/audio_path_report.md` already records that the audio message path this
  beep feeds ends in a queue whose receiver is absent from mas. The wait
  therefore has no one to complete it and must run to the 5000 ms timeout,
  taking the `0x10768bb8` path (`*out = 3`, "BeepPlay was timeout.").
* The hook sits in the render path — the call site is the display setup at
  `0x1073ece0..0x1073ecf6` (`bl 0x1034c1f0` alloc, `str r6,[r0]`, then
  `mov r1,r0; mov r0,r6; bl 0x1073eafe`). A 5-second synchronous wait there
  stops that thread from reaching `0x1073ecfc` and the rest of the frame
  submission, which is the observed "uploads stop at 12" of `dist/beep4`
  (beep4 = this same stub with APSR removed and BeepPlay kept).

**Verdict: blocking; dropped.** There is no non-blocking variant of this call
in the stub (the send at `0x10768b82` is only reachable *through* BeepPlay, and
BeepPlay always falls into the wait), so per the task rule it was removed from
both builds. That is why beep6 == noop6.

## 6. Why deleting the APSR pair is the fix (mechanism note)

The bisect is the evidence (probeC with the same push/pop and `sub sp` boots;
probeD, which only adds the APSR pair, dies): the pair is the fault. A
consistent mechanism from this image:

* The read side is native — the firmware itself uses `mrs rX, apsr`
  (`0x103b240a` `mrs r2, apsr`; `0x103a274c` `mrs r0, apsr`; 21 occurrences of
  that encoding in the image).
* The write side in the firmware's own save/restore helper is
  `msr apsr_nzcvq, r2` at **`0x103b2416`** (with `bic r2,r2,#0xf0000000` at
  `0x103b240e`), i.e. N,Z,C,V,Q only.
* The shipped stub used `msr apsr_nzcvqg, r4` (`0x1002d89e` in beep,
  `0x1002d89a` in noop), which additionally writes the GE field — the only
  place in the whole stub that touches architectural state outside
  `r0-r5`/`sp`. A core without GE flags takes an exception there, and the
  firmware's exception dispatcher (`0x101034a4..0x101034f8`, handler chain,
  `b 0x101034fc` at `0x101034fc` — a literal self-branch) has no handler for
  it and spins. This matches "no handler serviced this exception".

Labelled as inference: the bisect proves which bytes are fatal; the GE-field
read is the explanation, not an independently executed test. Either way the
pair is gone and the stub no longer touches APSR/GE at all.

## 7. Caveats

* Static only. No boot was executed for these builds (emulator in use), so
  "boots" rests on (i) the bisected-safe instruction set, (ii) byte-diff
  containment, and (iii) exact branch decoding — not on a run.
* Only `dist/beep6/` and `dist/noop6/` were created; `work/beep_fix/make_beep6.py`
  is new. No other file in the tree was modified.
* `dist/beep6` and `dist/noop6` being identical is a consequence of the
  BeepPlay verdict, not an oversight: the beep feature needs a non-blocking
  audio entry point that this image does not provide.

Reproduce: `python work/beep_fix/make_beep6.py`
