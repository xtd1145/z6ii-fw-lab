# Z6 II mas audio path (static, 2026-10-28)

**Result:** the mas-level playback engine is identified, but no routine is proven to be driven by the UI/shutter action. The only known user-action beep entry is still `0x10768b6e` (BeepPlay, strings "BeepPlay failed./was timeout." at 0x10768bc8), which calls the blocking queue begin at `0x10524005`.

Service IDs from the registry: SoundService `0x4314` (entry 0x1244d030), AudioService `0x4204` (entry 0x1244cbf8).

1. **`0x10242ee5`** — AUDIO_IF stream config. Found via `0x1037281d` -> `0x10242ee5`; `[stream+0x1c]` holds the AUDIO_IF base.
```
10242f61 ldr r1,[r5,#0x1c]
10242f63 ldrb r0,[r4,#8]
10242f65 str r0,[r1,#8]
10242f69 ldrb r0,[r4,#0xc]
10242f6b str r0,[r1,#0x38]
10242f6f ldrb r0,[r4,#2]
10242f71 str r0,[r1,#0x18]
```
Confidence: high (0.85) it programs audio format/channel/clock registers.

2. **`0x10242fc1`** — TX FIFO drain. Polls status `[hw+0x14]` and writes 16-bit samples to `[hw]`.
```
10243043 ldrh lr,[ip],#2
10243047 str.w lr,[r3]
10243055 ldr.w lr,[r1]
10243059 lsls.w lr,lr,#0x1b
1024305d bpl #0x10243043
```
Confidence: high (0.85) this is the sample FIFO.

3. **`0x1037281d`** — playback start. Divides sample rate (`0x186a0`, 5 MHz `0x004c4b40`) and calls `0x10242ee5`; 11 callers, incl. `0x103711cc` (AudioService start) and `0x1036a0be` (PCM player). Confidence: medium-high (0.7).
High-level PCM player `0x10302521` (buffer+len, start/stop click `0x1036a1a3`) is reachable via thunks `0x102707f8` -> `0x102dcdba`; UI binding unproven (0.55).

Unexplored leads:
- SoundService server is absent from mas: all 24 xrefs to queue `0xc1811b44` are client stubs `0x103a6128-0x103a669c`; check SLA "CSoundService" `0x10871aff` or the task table (`SNDSRV 0x1244d8a4`, `BPSNDPLY 0x1244da9c`, no static xref).
- Beep method: `0x04190033` -> dispatcher `0x1037647d` -> `0x102606e3` case 1 -> `0x102b432d` posts msgid `0x4204010f` to `0xc1864570`; receiver absent in mas (11 xrefs only in `0x102b42e4-0x102b43b6`).
- AUDIO_IF base at `[stream+0x1c]` not resolved statically (stream table `0xc18ad658`, structs `0xc1802090`; no initializer xref).
