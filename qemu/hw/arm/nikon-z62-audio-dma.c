/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Explicit laboratory sink for the observed Z62 bank1 audio TX DMA.
 * Geometry follows original 1036FFA0/10370692 and session29 register writes.
 * Completion state/timing and 48k stereo capture are functional assumptions,
 * not recovered silicon timing. Other DMA destinations are rejected. */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "system/address-spaces.h"

#define TYPE_Z62_AUDIO_DMA "nikon-z62-audio-dma"
OBJECT_DECLARE_SIMPLE_TYPE(Z62AudioDMA, Z62_AUDIO_DMA)
typedef struct AudioChannel {
    Z62AudioDMA *owner;
    unsigned index;
    QEMUTimer *timer;
    uint8_t *data;
    uint32_t bytes;
    bool pending;
} AudioChannel;
struct Z62AudioDMA {
    SysBusDevice parent_obj;
    MemoryRegion iomem, *root;
    AddressSpace memory;
    uint32_t regs[0x100 / 4], sample_rate;
    bool irq_pending[8];
    qemu_irq irqs[8];
    AudioChannel channels[8];
    char *capture_file;
    FILE *capture;
    uint64_t completed, captured, failed;
};

static bool audio_ram(Z62AudioDMA *s, uint32_t address, uint32_t bytes)
{
    uint64_t end = (uint64_t)address + bytes;
    if (end > UINT64_C(0x100000000) || (address >= 0x20000000 && address < 0x30000000)) {
        return false;
    }
    while (bytes) {
        hwaddr translated, available = bytes;
        MemoryRegion *mr = address_space_translate(&s->memory, address,
            &translated, &available, false, MEMTXATTRS_UNSPECIFIED);
        if (!available || !memory_region_is_ram(mr) || memory_region_is_rom(mr)) {
            return false;
        }
        available = MIN(available, bytes);
        address += available;
        bytes -= available;
    }
    return true;
}

static bool audio_capture(Z62AudioDMA *s, const uint8_t *data, uint32_t bytes)
{
    uint8_t header[44] = { 0 };
    if (!s->capture_file || s->captured + bytes > UINT32_MAX - 36) {
        return false;
    }
    if (!s->capture) {
        s->capture = fopen(s->capture_file, "w+b");
        if (!s->capture || fwrite(header, 1, 44, s->capture) != 44) {
            return false;
        }
    }
    if (fseek(s->capture, 0, SEEK_END) || fwrite(data, 1, bytes, s->capture) != bytes) {
        return false;
    }
    memcpy(header, "RIFF", 4); stl_le_p(header + 4, s->captured + bytes + 36);
    memcpy(header + 8, "WAVEfmt ", 8); stl_le_p(header + 16, 16);
    stw_le_p(header + 20, 1); stw_le_p(header + 22, 2);
    stl_le_p(header + 24, s->sample_rate); stl_le_p(header + 28, s->sample_rate * 4);
    stw_le_p(header + 32, 4); stw_le_p(header + 34, 16);
    memcpy(header + 36, "data", 4); stl_le_p(header + 40, s->captured + bytes);
    return !fseek(s->capture, 0, SEEK_SET) && fwrite(header, 1, 44, s->capture) == 44 && !fflush(s->capture);
}

static void audio_complete(void *opaque)
{
    AudioChannel *channel = opaque;
    Z62AudioDMA *s = channel->owner;
    unsigned n = channel->index, at = 4 + n * 4;
    channel->pending = false;
    if ((s->regs[0] & 0x90000000u) != 0x90000000u ||
        !audio_capture(s, channel->data, channel->bytes)) {
        s->failed++;
        s->regs[at] &= ~0x80000000u;
        g_clear_pointer(&channel->data, g_free);
        return;
    }
    s->captured += channel->bytes;
    s->completed++;
    s->regs[at] = (s->regs[at] & ~0x800fffffu) | 0x10000;
    s->irq_pending[n] = true;
    g_clear_pointer(&channel->data, g_free);
    qemu_set_irq(s->irqs[n], 1);
}

static void audio_start(Z62AudioDMA *s, unsigned n)
{
    AudioChannel *channel = &s->channels[n];
    uint32_t *r = s->regs + 4 + n * 4;
    uint32_t bytes = ((r[0] & 0xffff) + 1) * 4, source = r[2], target = r[3];
    unsigned port;
    uint32_t control = 0;
    if (channel->pending) {
        return;
    }
    if (target == 0x2c080104 || target == 0x2c08013c) {
        port = 0;
    } else if (target == 0x2c080204 || target == 0x2c08023c) {
        port = 1;
    } else {
        goto reject;
    }
    if ((s->regs[0] & 0x90000000u) != 0x90000000u ||
        (r[0] & 0x7ff00000u) != 0x03000000u ||
        (r[1] & ~0x1f0000u) != 0x29e00000u || (source & 3) ||
        !audio_ram(s, source, bytes) ||
        address_space_read(&s->memory, 0x2c080110 + port * 0x100,
            MEMTXATTRS_UNSPECIFIED, &control, 4) != MEMTX_OK ||
        (le32_to_cpu(control) & 0xa0000000u) != 0xa0000000u) {
        goto reject;
    }
    channel->data = g_malloc(bytes);
    if (address_space_read(&s->memory, source, MEMTXATTRS_UNSPECIFIED,
                           channel->data, bytes) != MEMTX_OK) {
        g_clear_pointer(&channel->data, g_free);
        goto reject;
    }
    channel->bytes = bytes;
    channel->pending = true;
    timer_mod(channel->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              MAX(UINT64_C(1000), (uint64_t)bytes * 1000000000 / (s->sample_rate * 4)));
    return;
reject:
    s->failed++;
    r[0] &= ~0x80000000u;
}

static uint64_t audio_read(void *opaque, hwaddr offset, unsigned size)
{
    Z62AudioDMA *s = opaque;
    uint32_t value = s->regs[offset / 4];
    if (offset >= 0x10 && offset < 0x90 && (offset & 0xf) == 4) {
        value = (value & ~0x10000u) | (s->irq_pending[(offset - 0x10) / 0x10] ? 0x10000 : 0);
    }
    return value;
}

static void audio_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    Z62AudioDMA *s = opaque;
    s->regs[offset / 4] = value;
    if (offset >= 0x10 && offset < 0x90) {
        unsigned n = (offset - 0x10) / 0x10;
        if ((offset & 0xf) == 4) {
            if (!(value & 0x10000)) {
                s->irq_pending[n] = false;
                qemu_set_irq(s->irqs[n], 0);
            }
            s->regs[offset / 4] &= ~0x10000u;
        } else if (!(offset & 0xf)) {
            if (value & 0x80000000u) {
                audio_start(s, n);
            } else {
                timer_del(s->channels[n].timer);
                s->channels[n].pending = false;
                g_clear_pointer(&s->channels[n].data, g_free);
            }
        }
    }
}
static const MemoryRegionOps audio_ops = {
    .read = audio_read, .write = audio_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4, .unaligned = false },
};

static void audio_reset(Object *obj, ResetType type)
{
    Z62AudioDMA *s = Z62_AUDIO_DMA(obj);
    memset(s->regs, 0, sizeof(s->regs));
    s->completed = s->captured = s->failed = 0;
    if (s->capture) { fclose(s->capture); s->capture = NULL; }
    for (unsigned n = 0; n < 8; n++) {
        timer_del(s->channels[n].timer); s->channels[n].pending = false;
        g_clear_pointer(&s->channels[n].data, g_free);
        s->irq_pending[n] = false; qemu_set_irq(s->irqs[n], 0);
    }
}
static void audio_realize(DeviceState *dev, Error **errp)
{
    Z62AudioDMA *s = Z62_AUDIO_DMA(dev);
    if (!s->root || !s->sample_rate || s->sample_rate > 192000) {
        error_setg(errp, "Audio DMA requires an engine memory view and bounded capture rate"); return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z62_AUDIO_DMA);
}
static void audio_unrealize(DeviceState *dev)
{
    Z62AudioDMA *s = Z62_AUDIO_DMA(dev);
    if (s->capture) { fclose(s->capture); s->capture = NULL; }
    for (unsigned n = 0; n < 8; n++) {
        timer_del(s->channels[n].timer); g_clear_pointer(&s->channels[n].data, g_free);
    }
    address_space_destroy(&s->memory);
}
static void audio_finalize(Object *obj)
{
    Z62AudioDMA *s = Z62_AUDIO_DMA(obj);
    for (unsigned n = 0; n < 8; n++) { timer_free(s->channels[n].timer); }
}
static void audio_init(Object *obj)
{
    Z62AudioDMA *s = Z62_AUDIO_DMA(obj);
    memory_region_init_io(&s->iomem, obj, &audio_ops, s, TYPE_Z62_AUDIO_DMA, 0x100);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    for (unsigned n = 0; n < 8; n++) {
        sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irqs[n]);
        s->channels[n].owner = s; s->channels[n].index = n;
        s->channels[n].timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, audio_complete, &s->channels[n]);
    }
    object_property_add_uint64_ptr(obj, "completed-transfers", &s->completed, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "captured-bytes", &s->captured, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "failed-transfers", &s->failed, OBJ_PROP_FLAG_READ);
}
static void audio_class_init(ObjectClass *oc, const void *data)
{
    static const Property props[] = {
        DEFINE_PROP_LINK("root", Z62AudioDMA, root, TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_STRING("capture-file", Z62AudioDMA, capture_file),
        DEFINE_PROP_UINT32("sample-rate", Z62AudioDMA, sample_rate, 48000),
    };
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = audio_realize; dc->unrealize = audio_unrealize;
    device_class_set_props(dc, props); dc->user_creatable = false;
    RESETTABLE_CLASS(oc)->phases.hold = audio_reset;
}
static void audio_register(void)
{
    static const TypeInfo info = {
        .name = TYPE_Z62_AUDIO_DMA, .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(Z62AudioDMA), .instance_init = audio_init,
        .instance_finalize = audio_finalize, .class_init = audio_class_init,
    };
    type_register_static(&info);
}
type_init(audio_register)
