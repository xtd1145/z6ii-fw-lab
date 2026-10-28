/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Nikon Z6 II C1.70 port. Image/code pool extents follow original scatter
 * records, code-relocation setup and observed MMU translations; see
 * docs/z62-port.md. Remaining lab windows are not silicon specifications. */
#ifndef NIKON_Z62_PROFILE_H
#define NIKON_Z62_PROFILE_H

#define Z62_MASTER_BYTES 38750138u
#define Z62_MASTER_SHA256 "bb83613121f843d65101da936d942acdc513d86dbd3abc26259d0d870d03f5d4"
#define Z62_SLAVE_BYTES 8832194u
#define Z62_SLAVE_SHA256 "3fd8c2cbdf57fadb03f6d69b596b58a8703efdd61ae82572d5afeea0241a9ed5"
#define Z62_CONTROLLER_BYTES 524290u
#define Z62_CONTROLLER_SHA256 "9c65cad554ed02f83d37fbc825a8eb9efe79e3f32f98894dd5dd73edee416983"

static const NikonZ72Window nikon_z62_windows[] = {
    {0x00000000, 0x00100000, "z62-auxiliary-local-memory-lab"},
    {0xC1000000, 0x00300000, "z62-auxiliary-shared-memory-lab"},
    {0x04008000, 0x00001000, "z62-boot-stack-lab"},
    {0x10000000, 0x04000000, "z62-master-lab"},
    /* Original physical scatter destinations, records 10034050..B0.
     * CPU virtual aliases are supplied by the original firmware's MMU. */
    {0x80000000, 0x00010000, "z62-physical-local-lab"},
    {0x80400000, 0x00340000, "z62-physical-work-buffer-lab"},
    {0x80800000, 0x02313880, "z62-physical-capture-buffer-lab"},
    {0x82C00000, 0x082BC400, "z62-physical-processing-buffer-lab"},
    /* Original scatter record 10034090; VA341F5400 translates to
     * PA8C8F5400 through both the ROM and active task section tables. */
    {0x8AF00000, 0x02044400, "z62-physical-image-buffer-lab"},
    {0x8D000000, 0x31D17000, "z62-physical-frame-buffer-lab"},
    {0xBF700000, 0x00824A00, "z62-physical-frame-tail-lab"},
    /* Original work-pool setup 103A58C8..594A: BEF00000+1100000.
     * Kind-1 allocations use 16 slots of 80000 bytes (103A5BB2..5C10).
     * This pool is also visible through the existing peer bus alias. */
    {0xC0000000, 0x00800000, "z62-kind1-work-pool-lab"},
    {0xC1800000, 0x00100000, "z62-scatter-lab"},
    {0xC1900000, 0x00700000, "z62-boot-clear-lab"},
    {0xC2000000, 0x00C00000, "z62-state-stack-lab"},
    {0xC2C00000, 0x03300000, "z62-runtime-heap-lab"},
    {0xC5F00000, 0x00100000, "z62-extra-init-lab"},
    {0xC6000000, 0x00F00000, "z62-startup-storage-lab"},
    {0xC6F00000, 0x00D00000, "z62-peer-alias-lab"},
    {0xC7C00000, 0x00100000, "z62-master-message-ram-lab"},
    {0xC7D00000, 0x02D00000, "z62-master-buffer-pool-lab"},
    /* 1036A488 configures ROM10000000 -> RAMCC200000, length 00900000.
     * Task page tables then remap 101/102/105 to CC3/CC4/CC7. */
    {0xCC200000, 0x00900000, "z62-code-relocation-lab"},
    /* Original physical scatter destinations, records 100340F0..34130.
     * CD9 includes command sources CE8DFF90/CE95FF90; CEA includes
     * the PA D0100000 surface seen through CPU VA36000000. */
    {0xCCB00000, 0x00DA0000, "z62-secondary-buffer-pool-lab"},
    {0xCD900000, 0x010E0000, "z62-tertiary-buffer-pool-lab"},
    {0xCEA00000, 0x063CB400, "z62-processing-buffer-pool-lab"},
    {0xD4E00000, 0x0883B000, "z62-image-buffer-pool-lab"},
    {0xDD700000, 0x228C2800, "z62-frame-buffer-pool-lab"},
    {0x2C0A0000, 0x00001000, "z62-console-companion-lab"},
    {0x2C010000, 0x00080000, "z62-release-block-lab"},
    {0x20100000, 0x06F00000, "z62-runtime-window-lab"},
    {0x40100000, 0x00100000, "z62-harness-lab"},
};

static const NikonZ72Window nikon_z62_slave_windows[] = {
    {0x04008000, 0x00001000, "z62-slave-boot-stack-lab"},
    {0x10000000, 0x04000000, "z62-slave-code-lab"},
    {0xC1000000, 0x00A00000, "z62-slave-state-stack-lab"},
    {0xC1A00000, 0x03600000, "z62-slave-runtime-heap-lab"},
    {0xC5000000, 0x00100000, "z62-slave-extra-init-lab"},
    {0xC5100000, 0x00F00000, "z62-slave-startup-storage-lab"},
    {0xC6F00000, 0x00D00000, "z62-slave-peer-alias-lab"},
    {0xC6000000, 0x00100000, "z62-slave-message-ram-lab"},
    /* SLA scatter @100340A0 clears C6004000..C6FC313F. Keep the
     * shared message window above, but back the previously missing span. */
    {0xC6100000, 0x00E00000, "z62-slave-scatter-tail-lab"},
    {0x2C0A0000, 0x00001000, "z62-slave-console-companion-lab"},
    {0x2C010000, 0x00080000, "z62-slave-release-block-lab"},
    {0x20100000, 0x06F00000, "z62-slave-runtime-window-lab"},
    {0x40100000, 0x00100000, "z62-slave-harness-lab"},
};
#endif
