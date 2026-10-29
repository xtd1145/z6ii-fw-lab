# Attribution and licensing

Portions of this project are adapted from
[WonderLantern Z6 QEMU](https://github.com/WonderLantern/z6-qemu) and
[WonderLantern Z7 QEMU](https://github.com/WonderLantern/z7-qemu), including work
by R3tr0ooo and their contributors. We gratefully acknowledge their camera
emulation research, GPU implementation, build tools and regression tests.
Their attribution and GPL notices are retained. Upstream revisions and source
hashes are recorded in [reference-sources.json](docs/reference-sources.json).

The Z72 machine, firmware input guards and execution experiments target Nikon
Z7 II C 1.70. They implement a limited instruction laboratory, not a complete
EXPEED hardware specification.

The local Z6 II extension adds a separate Z62 machine/profile and an LCD
device. Banked Y/UV scanout and YYUV422A overlay composition in
`qemu/nikon-z62-lcd.c` adapt the `nikon_lcd_present` and `nikon_lcd_overlay`
implementations from WonderLantern/z6-qemu commit
`419a1f2a91a1f2d3a0209027c29606475f7fe26f`. Z62 memory addresses and image
identities were initially selected from the supplied 2026-10-25 handoff;
physical pools, code relocation, sensor-path registers and MENU input were
then independently checked against Z62 C1.70 instructions and runtime state.
The base Z72 checkout is
JKWTCN/z72-qemu commit `f5f4ba31b324de5abf6f24d399afff878ffd949a`.

The default-off CAPSEQ actuator in `qemu/nikon-z62-capseq.h` adapts
Fly-beep-256/z62_qemu's `nikon-z6ii-capseq-actuator.h`, commit
`1a79b9dbdc7bef89f942baf0335b80cb6ba47e71` (GPL-2.0-or-later).
`qemu/nikon-z62-isp-input-core.h` imports the same commit's
`nikon-z6ii-isp-frame-core.h` without core changes. Local wiring, properties,
traces and tests were added on 2026-10-28. These input/handshake hypotheses
do not claim recovered sensor pixels, DMA, mechanical timing or pin polarity.
Upstream source and license are retained in the integration experiment.

Overlay, tools, tests and documentation are GPL-2.0-or-later unless stated
otherwise; see COPYING. QEMU retains its per-file licenses, with the upstream
notice retained in LICENSES/QEMU-LICENSE.txt. No project license grants any
right in Nikon firmware; firmware copyright remains with its rights holders,
and each user must obtain firmware legally and separately. Nikon is not
affiliated with this independent project.

Original firmware and generated candidate binaries are local inputs/outputs,
excluded from the source inventory. Update-package decoding, extraction and
repacking tooling, and verbatim stock byte excerpts are likewise not part of
this repository; firmware windows are recorded as offsets, lengths and hashes,
and single instructions as semantic encodings. This project does not operate
a physical camera.

The integrated Windows laboratory is based on xtd1145/z6ii-fw-lab commit fc693ad51376e47fd2d5afcbc7da737673ffc781, retaining its machine diagnostics, experimental fixtures and web console. Local NOR Boot, NVMe, audio DMA and opt-in input models were merged on 2026-10-29. QEMU 11.1.2 host adaptations are recorded in qemu/host-compat.patch.
