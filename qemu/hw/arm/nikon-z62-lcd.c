/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Z6 II LCD register/IRQ layout from the Z62 handoff. Banked Y/UV and
 * YYUV422A overlay composition follow WonderLantern/z6-qemu, nikon-z6.c
 * (nikon_lcd_present / nikon_lcd_overlay). Register compatibility with
 * Z62 is experimental until observed in a complete firmware frame. */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "system/address-spaces.h"
#include "nikon-gpu.h"

#define TYPE_Z62_LCD "nikon-z62-lcd"
#define LCD_BYTES 0xc000
OBJECT_DECLARE_SIMPLE_TYPE(Z62LCD, Z62_LCD)

struct Z62LCD {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    MemoryRegion *root;
    AddressSpace memory;
    NikonGPU reader;
    QemuConsole *console;
    QEMUTimer *timer;
    qemu_irq irqs[3];
    uint32_t regs[LCD_BYTES / 4];
    uint32_t frames;
};

static void lcd_irqs(Z62LCD *s)
{
    bool level[3] = { false };
    for (unsigned inst = 0; inst < 3; inst++) {
        uint32_t *r = s->regs + inst * 0x4000 / 4;
        unsigned mode = r[0x1100 / 4] & 3;
        if (mode && (r[0x1210 / 4] & r[0x1214 / 4])) {
            level[mode - 1] = true;
        }
    }
    for (unsigned line = 0; line < 3; line++) {
        qemu_set_irq(s->irqs[line], level[line]);
    }
}

static uint64_t lcd_read(void *opaque, hwaddr offset, unsigned size)
{
    return Z62_LCD(opaque)->regs[offset / 4];
}

static void lcd_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    Z62LCD *s = opaque;
    if (offset % 0x4000 == 0x1214) {
        s->regs[offset / 4] &= ~(uint32_t)value;
    } else {
        s->regs[offset / 4] = value;
    }
    if (getenv("Z62_LCD_TRACE")) {
        fprintf(stderr, "Z62_LCD_WRITE offset=0x%04x value=0x%08x\n",
                (unsigned)offset, (unsigned)value);
    }
    lcd_irqs(s);
}

static const MemoryRegionOps lcd_ops = {
    .read = lcd_read,
    .write = lcd_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4, .unaligned = false },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static unsigned lcd_overlay(Z62LCD *s, uint32_t *r, unsigned plane,
                            uint8_t *out, unsigned stride, unsigned w, unsigned h)
{
    unsigned base = plane ? 0x3000 : 0x2000;
    if (!(r[(base + 0x110) / 4] & 1) ||
        r[(base + 0x200) / 4] != 0x30006) {
        return 0;
    }
    unsigned bank = (r[(base + 0x214) / 4] >> 8) & 3;
    r[(base + 0x214) / 4] =
        (r[(base + 0x214) / 4] & ~0x30000U) | bank << 16;
    unsigned regions = 0;
    for (unsigned region = 0; region < 10; region++) {
        if (!(r[(base + 0x5c0) / 4] & (1U << region))) {
            continue;
        }
        uint32_t size = r[(base + 0x400) / 4 + region];
        uint32_t xy = r[(base + 0x580) / 4 + region];
        unsigned rw = size & 0xffff, rh = (size >> 16) & 0x3fff;
        unsigned x0 = xy & 0xffff, y0 = (xy >> 16) & 0x3fff;
        unsigned cs = r[(base + 0x500) / 4 + region];
        unsigned as = r[(base + 0x540) / 4 + region];
        uint32_t ca = region ? r[(base + 0x47c) / 4 + region] :
                              r[(base + 0x440) / 4 + bank];
        uint32_t aa = r[(base + 0x4c0) / 4 + region];
        if (!rw || !rh || rw > 4096 || rh > 4096 || x0 >= w || y0 >= h ||
            cs < ((rw + 1) & ~1U) * 2 || as < rw || cs > 16384 || as > 16384 ||
            !nikon_gpu_range(&s->reader, ca, (uint64_t)cs * rh, false) ||
            !nikon_gpu_range(&s->reader, aa, (uint64_t)as * rh, false)) {
            continue;
        }
        g_autofree uint8_t *color = g_malloc(cs), *alpha = g_malloc(as);
        for (unsigned y = 0; y < MIN(rh, h - y0); y++) {
            nikon_gpu_read(&s->reader, ca + (uint64_t)y * cs, color, cs);
            nikon_gpu_read(&s->reader, aa + (uint64_t)y * as, alpha, as);
            for (unsigned x = 0; x < MIN(rw, w - x0); x++) {
                uint8_t top[4];
                nikon_gpu_pixel_read(color, alpha, 0x107, false, x, top);
                uint8_t *dest = out + (size_t)(y + y0) * stride + (x + x0) * 4;
                uint32_t old = ldl_p(dest);
                unsigned red = (top[0] * top[3] + ((old >> 16) & 255) *
                                (255 - top[3]) + 127) / 255;
                unsigned green = (top[1] * top[3] + ((old >> 8) & 255) *
                                  (255 - top[3]) + 127) / 255;
                unsigned blue = (top[2] * top[3] + (old & 255) *
                                 (255 - top[3]) + 127) / 255;
                stl_p(dest, (red << 16) | (green << 8) | blue);
            }
        }
        regions++;
    }
    return regions;
}

static void lcd_present(void *opaque)
{
    Z62LCD *s = opaque;
    uint32_t *r = s->regs + 0x4000 / 4;
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 60000000);
    /* Functional timing, not a recovered physical refresh rate. */
    if (!(r[0x1200 / 4] & 1)) {
        return;
    }
    r[0x1214 / 4] |= 1U << 16;
    lcd_irqs(s);
    unsigned w = r[0x204 / 4] & 0xffff, h = r[0x204 / 4] >> 16;
    if (!w || !h || (w & 1) || w > 2048 || h > 2048) {
        return;
    }
    DisplaySurface *surface = qemu_console_surface(s->console);
    if (surface_width(surface) != w || surface_height(surface) != h) {
        qemu_console_resize(s->console, w, h);
        surface = qemu_console_surface(s->console);
    }
    unsigned stride = surface_stride(surface);
    uint8_t *out = surface_data(surface);
    memset(out, 0, (size_t)stride * h);
    unsigned bank = (r[0x260 / 4] >> 8) & 3;
    uint32_t ya = r[0x210 / 4 + bank], ua = r[0x220 / 4 + bank];
    unsigned ys = r[0x240 / 4], us = r[0x250 / 4];
    bool nv12 = r[0x200 / 4] == 0x300004;
    unsigned uvh = nv12 ? (h + 1) / 2 : h;
    bool video = (r[0x110 / 4] & 1) &&
        (r[0x200 / 4] == 0x300000 || nv12) &&
        ys >= w && us >= w && ys <= 16384 && us <= 16384 &&
        nikon_gpu_range(&s->reader, ya, (uint64_t)ys * h, false) &&
        nikon_gpu_range(&s->reader, ua, (uint64_t)us * uvh, false);
    if (video) {
        g_autofree uint8_t *yp = g_malloc((size_t)ys * h);
        g_autofree uint8_t *up = g_malloc((size_t)us * uvh);
        nikon_gpu_read(&s->reader, ya, yp, (size_t)ys * h);
        nikon_gpu_read(&s->reader, ua, up, (size_t)us * uvh);
        for (unsigned y = 0; y < h; y++) {
            for (unsigned x = 0; x < w; x++) {
                uint8_t pixel[4];
                size_t uv = (size_t)(nv12 ? y / 2 : y) * us + (x & ~1U);
                nikon_yuv_rgb(yp[(size_t)y * ys + x], up[uv], up[uv + 1], pixel);
                stl_p(out + (size_t)y * stride + x * 4,
                      (pixel[0] << 16) | (pixel[1] << 8) | pixel[2]);
            }
        }
    }
    unsigned overlays = lcd_overlay(s, r, 0, out, stride, w, h) +
                        lcd_overlay(s, r, 1, out, stride, w, h);
    qemu_console_update(s->console, 0, 0, w, h);
    if ((video || overlays) && s->frames++ == 0) {
        fprintf(stderr, "Z62_LCD_SCANOUT size=%ux%u bank=%u video=%u overlays=%u\n",
                w, h, bank, video, overlays);
    }
}

static void lcd_reset(Object *obj, ResetType type)
{
    Z62LCD *s = Z62_LCD(obj);
    memset(s->regs, 0, sizeof(s->regs));
    s->frames = 0;
    lcd_irqs(s);
    DisplaySurface *surface = qemu_console_surface(s->console);
    memset(surface_data(surface), 0,
           (size_t)surface_stride(surface) * surface_height(surface));
    qemu_console_update(s->console, 0, 0, surface_width(surface), surface_height(surface));
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 60000000);
}

static void lcd_realize(DeviceState *dev, Error **errp)
{
    Z62LCD *s = Z62_LCD(dev);
    if (!s->root) {
        error_setg(errp, "Z62 LCD requires the master engine memory view");
        return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z62_LCD);
    s->reader.memory = &s->memory;
    static const GraphicHwOps ops = { 0 };
    s->console = qemu_graphic_console_create(dev, 0, &ops, s);
    qemu_console_resize(s->console, 1024, 680);
}

static void lcd_init(Object *obj)
{
    Z62LCD *s = Z62_LCD(obj);
    memory_region_init_io(&s->iomem, obj, &lcd_ops, s, TYPE_Z62_LCD, LCD_BYTES);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    for (unsigned line = 0; line < 3; line++) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irqs[line]);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, lcd_present, s);
}

static void lcd_unrealize(DeviceState *dev)
{
    Z62LCD *s = Z62_LCD(dev);
    timer_del(s->timer);
    if (s->console) {
        qemu_graphic_console_close(s->console);
        address_space_destroy(&s->memory);
    }
}

static void lcd_finalize(Object *obj)
{
    timer_free(Z62_LCD(obj)->timer);
}

static void lcd_class_init(ObjectClass *klass, const void *data)
{
    static const Property properties[] = {
        DEFINE_PROP_LINK("root", Z62LCD, root, TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_UINT32("scanned-frames", Z62LCD, frames, 0),
    };
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = lcd_realize;
    dc->unrealize = lcd_unrealize;
    dc->user_creatable = false;
    device_class_set_props(dc, properties);
    RESETTABLE_CLASS(klass)->phases.hold = lcd_reset;
}

static void lcd_register(void)
{
    static const TypeInfo info = {
        .name = TYPE_Z62_LCD,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(Z62LCD),
        .instance_init = lcd_init,
        .instance_finalize = lcd_finalize,
        .class_init = lcd_class_init,
    };
    type_register_static(&info);
}
type_init(lcd_register)
