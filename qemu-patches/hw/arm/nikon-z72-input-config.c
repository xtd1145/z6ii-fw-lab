/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Input processing configuration storage recovered from the firmware.
 * The default-off laboratory metadata source signals an explicit frame
 * boundary; this device does not execute pixel processing or pixel DMA. */
#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "migration/vmstate.h"

#define TYPE_Z72_INPUT_CONFIG "nikon-z72-input-config"
OBJECT_DECLARE_SIMPLE_TYPE(Z72InputConfig, Z72_INPUT_CONFIG)

/* Register offsets below are relative to 0x20068000. The exported region
 * starts at +0x400, beyond the existing input controls and event banks. */
#define CONFIG_FIRST 0x400
#define CONFIG_LIMIT 0x4000

struct Z72InputConfig {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t registers[CONFIG_LIMIT / 4];
    bool z62_layout;
    bool laboratory_metadata_completion;
    bool frame_end_level;
    uint64_t metadata_completions;
    qemu_irq metadata_irq;
};

static void input_frame_end(void *opaque, int line, int level)
{
    Z72InputConfig *s = opaque;
    bool edge = level && !s->frame_end_level;
    s->frame_end_level = level != 0;
    /* Opt-in metadata-only experiment. Z62 mode0 explicitly enables
     * 20069F00 bits0/4; input bank0 bit6 then consumes the prepared
     * frame-result object. No pixel, Bayer DMA or ISP output is produced.
     * The association with a supplied frame-end clock is functional. */
    if (edge && s->z62_layout && s->laboratory_metadata_completion &&
        (s->registers[0x1f00 / 4] & 0x11) == 0x11) {
        qemu_set_irq(s->metadata_irq, 1);
        qemu_set_irq(s->metadata_irq, 0);
        s->metadata_completions++;
    }
}

static bool z62_input_result(hwaddr offset)
{
    if ((offset >= 0x23e0 && offset <= 0x23ec) || offset == 0x3404 ||
        offset == 0x3988 || (offset >= 0x3b70 && offset <= 0x3b7c) ||
        offset == 0x3c78 || offset == 0x3c7c ||
        offset == 0x3d78 || offset == 0x3d7c) {
        return true;
    }
    for (unsigned instance = 0; instance < 2; instance++) {
        unsigned base = instance ? 0x3e00 : 0x2000;
        if (offset == base + 0x44 ||
            (offset >= base + 0x100 && offset <= base + 0x12c)) {
            return true;
        }
    }
    return offset >= 0x2400 && offset < 0x2e00 &&
           ((offset & 0xff) == 0x70 || (offset & 0xff) == 0x74 ||
            (offset & 0xff) == 0x78 || (offset & 0xff) == 0x7c);
}

static bool input_config_accepts(void *opaque, hwaddr offset, unsigned size,
                                 bool is_write, MemTxAttrs attrs)
{
    Z72InputConfig *s = opaque;
    static const struct { unsigned first, last; } ranges[] = {
        { 0x500, 0x500 }, { 0x508, 0x81c },
        { 0xa00, 0xafc }, { 0xc00, 0xe3c },
        { 0xf00, 0x100c }, { 0x1100, 0x117c },
        { 0x1500, 0x1530 }, { 0x1600, 0x17fc },
        { 0x1d00, 0x1efc }, { 0x1f00, 0x1f00 },
        { 0x3310, 0x33bc }, { 0x33c0, 0x33c0 },
        { 0x33c4, 0x33c8 }, { 0x33cc, 0x33fc },
        { 0x3400, 0x3400 },
    };

    offset += CONFIG_FIRST;
    if (size != 4 || (offset & 3)) {
        return false;
    }
    if (s->z62_layout) {
        if (z62_input_result(offset)) {
            return true;
        }
        /* 10209EF6/10209FEA use two instance offsets, 0 and 1E00,
         * from original objects C18067B4/C18067C4. */
        for (unsigned instance = 0; instance < 2; instance++) {
            unsigned base = instance ? 0x3e00 : 0x2000;
            if ((offset >= base && offset <= base + 0x24) ||
                offset == base + 0x40) {
                return true;
            }
        }
        /* 10208CB0..10208F38: direct configuration and LUT writes.
         * BA34..BA50 is the observed n=1 table (C26A090A); larger
         * counts have not yet established additional legal addresses. */
        if ((offset >= 0x3800 && offset <= 0x3984) ||
            (offset >= 0x3a00 && offset <= 0x3a50) ||
            (offset >= 0x3a94 && offset <= 0x3ab8)) {
            return true;
        }
        switch (offset) {
        case 0x3b18: case 0x3b24: case 0x3b3c: case 0x3b44:
        case 0x3b48: case 0x3b4c: case 0x3b50: case 0x3b60:
        case 0x3b80: case 0x3c50: case 0x3c60: case 0x3c80:
        case 0x3d50: case 0x3d60: case 0x3d80:
            return true;
        }
    }
    /* Z62 1020B130 copies exactly 129 halfword table entries as word
     * writes to 2006B000..2006B200. This is a LUT, not frame data. */
    if (s->z62_layout && offset >= 0x3000 && offset <= 0x3200) {
        return true;
    }
    /* Z62 1020AEA8..1020B130 program 93 contiguous configuration words;
     * 1020AF08 reads four independent result slots at +23E0..23EC. */
    if (s->z62_layout && ((offset >= 0x2200 && offset <= 0x2370) ||
                         (offset >= 0x23e0 && offset <= 0x23ec))) {
        return true;
    }
    for (unsigned i = 0; i < G_N_ELEMENTS(ranges); i++) {
        if (offset >= ranges[i].first && offset <= ranges[i].last) {
            return true;
        }
    }
    if (offset >= 0x1800 && offset < 0x1c00) {
        unsigned channel = (offset - 0x1800) / 0x100;
        unsigned reg = offset & 0xff;
        bool control = !(channel & 1) &&
            (reg == 0x14 || reg == 0x18 || reg == 0x3c || reg == 0x40 ||
             reg == 0x44 || reg == 0x48 || reg == 0x4c);

        return control || reg == 8 || (reg >= 0x50 && reg <= 0x6c) ||
               (channel == 2 && reg == 0x24) ||
               (channel >= 2 && reg == 0x80);
    }
    if ((offset >= 0x2400 && offset < 0x2e00) ||
        (offset >= 0x3400 && offset < 0x3e00)) {
        unsigned channel = ((offset & 0xfff) - 0x400) / 0x100;
        unsigned reg = offset & 0xff;
        bool planes = channel == 2 || channel == 3 || channel == 4 || channel == 9;

        return reg == 0x14 || reg == 0x18 || reg == 0x24 || reg == 0x3c ||
               reg == 0x40 || reg == 0x44 || reg == 0x48 || reg == 0x4c ||
               reg == 0x50 || reg == 0x60 || reg == 0x74 || reg == 0x80 ||
               (planes && ((reg >= 0x54 && reg <= 0x5c) ||
                           (reg >= 0x64 && reg <= 0x6c)));
    }
    return false;
}

static uint64_t input_config_read(void *opaque, hwaddr offset, unsigned size)
{
    Z72InputConfig *s = opaque;

    return s->registers[(offset + CONFIG_FIRST) / 4];
}

static MemTxResult input_config_write(void *opaque, hwaddr offset, uint64_t value,
                                      unsigned size, MemTxAttrs attrs)
{
    Z72InputConfig *s = opaque;

    offset += CONFIG_FIRST;
    if (s->z62_layout && z62_input_result(offset)) {
        return MEMTX_OK;
    }
    if (offset == 0x500 && value > 0x3fff) {
        return MEMTX_ERROR;
    }
    s->registers[offset / 4] = value;
    return MEMTX_OK;
}

static const MemoryRegionOps input_config_ops = {
    .read = input_config_read,
    .write_with_attrs = input_config_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4,
               .accepts = input_config_accepts },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void input_config_reset(Object *obj, ResetType type)
{
    Z72InputConfig *s = Z72_INPUT_CONFIG(obj);

    memset(s->registers, 0, sizeof(s->registers));
    s->frame_end_level = false;
    s->metadata_completions = 0;
    qemu_set_irq(s->metadata_irq, 0);
}

static const VMStateDescription input_config_vmstate = {
    .name = TYPE_Z72_INPUT_CONFIG,
    .version_id = 2,
    .minimum_version_id = 2,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(registers, Z72InputConfig, CONFIG_LIMIT / 4),
        VMSTATE_BOOL(frame_end_level, Z72InputConfig),
        VMSTATE_UINT64(metadata_completions, Z72InputConfig),
        VMSTATE_END_OF_LIST()
    },
};

static void input_config_init(Object *obj)
{
    Z72InputConfig *s = Z72_INPUT_CONFIG(obj);

    memory_region_init_io(&s->iomem, obj, &input_config_ops, s,
                          TYPE_Z72_INPUT_CONFIG, CONFIG_LIMIT - CONFIG_FIRST);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    qdev_init_gpio_in_named(DEVICE(s), input_frame_end, "frame-end", 1);
    qdev_init_gpio_out_named(DEVICE(s), &s->metadata_irq, "metadata-ready", 1);
    object_property_add_uint64_ptr(obj, "metadata-completions",
                                  &s->metadata_completions, OBJ_PROP_FLAG_READ);
}

static void input_config_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);

    dc->vmsd = &input_config_vmstate;
    static const Property properties[] = {
        DEFINE_PROP_BOOL("z62-layout", Z72InputConfig, z62_layout, false),
        DEFINE_PROP_BOOL("laboratory-metadata-completion", Z72InputConfig,
                         laboratory_metadata_completion, false),
    };
    device_class_set_props(dc, properties);
    dc->user_creatable = false;
    RESETTABLE_CLASS(oc)->phases.hold = input_config_reset;
}

static const TypeInfo input_config_info = {
    .name = TYPE_Z72_INPUT_CONFIG,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Z72InputConfig),
    .instance_init = input_config_init,
    .class_init = input_config_class_init,
};

static void input_config_register_types(void)
{
    type_register_static(&input_config_info);
}
type_init(input_config_register_types)
