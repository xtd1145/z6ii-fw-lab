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
#include "migration/vmstate.h"
#include "nikon-gpu.h"
#include "nikon-z62-sensor-sim.h"

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
    bool test_frame;
    uint64_t test_seq;
    uint32_t video_y, video_uv, video_format, video_w, video_h;
    uint32_t trace_ya, trace_ua;
    unsigned video_changes;
    unsigned overlay_traces;
    int overlay_bank;   /* lab: force one live-view scan-out bank, -1 = guest */
    int attitude;
    bool e7_ui;
    uint8_t e7_tab;
    uint8_t e7_row;
    uint32_t e7_cells;
    uint32_t scanout_w, scanout_h;
    /* Frame period in milliseconds. The panel model composes a whole frame
     * per tick rather than scanning lines, so this is the one timing knob the
     * guest can actually observe. It used to be a hard-coded 60 ms, which
     * made frame-rate-dependent behaviour untestable: any firmware whose
     * cadence interacted with the refresh period looked identical here. */
    uint32_t frame_ms;
    /* Live-view plane filling. With real NOR parameters the firmware runs its
     * normal live-view path: LCD 0x4110=1, format 0x300000, Y at 0xdcf33000,
     * UV at 0xdcfdd800, both strides 0x400. Every register is right but the
     * planes are blank, so the panel renders black. When enabled this pulls a
     * frame from the sensor model into those planes. Off by default so the
     * laboratory behaviour is unchanged unless asked for. */
    Z62SensorSim *sensor_sim;
    bool live_view_fill;
    bool live_view_filled;
    uint8_t e7_state[160];
};

/* Lab-side attitude. The Z6 II firmware never turns its own UI, so portrait
 * preview has to come from the emulator: "auto" trusts whatever geometry the
 * guest programmed, the two portrait values rotate a landscape frame 90
 * degrees in opposite directions, and a frame the guest already drew tall is
 * left alone.
 */
enum { ATT_AUTO, ATT_LANDSCAPE, ATT_PORTRAIT, ATT_REVERSE_PORTRAIT };

static const char *const att_names[] = {
    [ATT_AUTO] = "auto",
    [ATT_LANDSCAPE] = "landscape",
    [ATT_PORTRAIT] = "portrait",
    [ATT_REVERSE_PORTRAIT] = "reverse-portrait",
};

static int att_parse(const char *name)
{
    for (unsigned i = 0; i < ARRAY_SIZE(att_names); i++) {
        if (!g_ascii_strcasecmp(name, att_names[i])) {
            return (int)i;
        }
    }
    return -1;
}

static bool att_turn(int attitude, unsigned w, unsigned h, bool *reverse)
{
    *reverse = attitude == ATT_REVERSE_PORTRAIT;
    return (attitude == ATT_PORTRAIT || attitude == ATT_REVERSE_PORTRAIT) &&
           w > h;
}

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
    Z62LCD *s = Z62_LCD(opaque);
    if (getenv("Z62_LCD_TRACE")) {
        static unsigned traced;
        if (traced < 300) {
            traced++;
            fprintf(stderr, "Z62_LCD rd off=%04" HWADDR_PRIx " val=%08x\n",
                    offset, s->regs[offset / 4]);
        }
    }
    return s->regs[offset / 4];
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
    if (s->overlay_bank >= 0) {
        bank = s->overlay_bank & 3;
    }
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
        if (!plane && region == 0 && getenv("Z62_LCD_FORCE_LV")) {
            /* Lab: scan the plane live replay actually refreshes, with its own
             * alpha plane (same 0x155000 plane gap the guest uses). */
            ca = nikon_gpu_lab_lv_plane ? nikon_gpu_lab_lv_plane : 0xdc82b000;
            aa = ca + 0x155000;
        }
        if (!rw || !rh || rw > 4096 || rh > 4096 || x0 >= w || y0 >= h ||
            cs < ((rw + 1) & ~1U) * 2 || as < rw || cs > 16384 || as > 16384 ||
            !nikon_gpu_range(&s->reader, ca, (uint64_t)cs * rh, false) ||
            !nikon_gpu_range(&s->reader, aa, (uint64_t)as * rh, false)) {
            continue;
        }
        if (getenv("Z62_LCD_OVERLAY_TRACE") && s->overlay_traces < 4) {
            s->overlay_traces++;
            fprintf(stderr, "Z62_LCD_OVERLAY plane=%u region=%u ca=%08x aa=%08x "
                    "cs=%u as=%u %ux%u at %u,%u\n", plane, region, ca, aa,
                    cs, as, rw, rh, x0, y0);
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

/* Laboratory YUV422 source. The camera normally fills the video plane from
 * the sensor/ISP, which this board does not model. This writes a synthetic
 * frame into the plane the LCD is about to scan, so the display and any guest
 * consumer of that buffer see real pixels. It is not recovered sensor data. */
static void lcd_test_frame(Z62LCD *s, uint32_t ya, uint32_t ua,
                           unsigned ys, unsigned us, unsigned w, unsigned h,
                           bool nv12, uint64_t seq)
{
    static const uint8_t bars[8][3] = {
        {235, 235, 235}, {235, 235, 16}, {16, 235, 235}, {16, 235, 16},
        {235, 16, 235}, {235, 16, 16}, {16, 16, 235}, {16, 16, 16},
    };
    g_autofree uint8_t *yrow = g_malloc(ys);
    g_autofree uint8_t *uvrow = g_malloc(us);
    unsigned barw = MAX(1, w / 8);
    int mx = (int)(seq * 8 % (w + 32)) - 24;
    unsigned by = (seq * 3) % h;
    for (unsigned y = 0; y < h; y++) {
        for (unsigned x = 0; x < w; x += 2) {
            uint8_t rgb0[3], rgb1[3], a[3], b[3];
            memcpy(rgb0, bars[MIN(x / barw, 7)], 3);
            memcpy(rgb1, bars[MIN((x + 1) / barw, 7)], 3);
            if ((int)x >= mx && (int)x < mx + 32) {
                rgb0[0] = rgb0[1] = rgb0[2] = 255;
                rgb1[0] = rgb1[1] = rgb1[2] = 255;
            }
            if (y >= by && y < by + 8) {
                rgb0[0] = rgb1[0] = 255;
                rgb0[1] = rgb1[1] = 160;
                rgb0[2] = rgb1[2] = 0;
            }
            nikon_rgb_yuv(rgb0, a);
            nikon_rgb_yuv(rgb1, b);
            yrow[x] = a[0];
            yrow[x + 1] = b[0];
            uvrow[x] = (a[1] + b[1] + 1) / 2;
            uvrow[x + 1] = (a[2] + b[2] + 1) / 2;
        }
        nikon_gpu_write(&s->reader, ya + (uint64_t)y * ys, yrow, w);
        if (!nv12 || !(y & 1)) {
            nikon_gpu_write(&s->reader, ua + (uint64_t)(nv12 ? y / 2 : y) * us,
                            uvrow, w);
        }
    }
    if (!seq) {
        fprintf(stderr, "Z62_LCD_TEST_FRAME ya=%08x ua=%08x %ux%u stride=%u/%u nv12=%u\n",
                ya, ua, w, h, ys, us, nv12);
    }
    s->test_seq++;
}

/* EXPEED 7 chrome, taken from a lossless Z 8 emulator render at 1024x682
 * (the same panel size this board scans out): tab cells 91x91 from x=6,
 * title bar y 0..90, seven rows 82 px tall spanning x 108..1006, scrollbar
 * hard against the right edge. Colours sampled from that render: rows
 * 22 22 22, title bar 2b 2b 2b, highlighted row fb d6 24, live tab 58 70 00,
 * values be be be on black.
 */
#define E7_BG       0x000000u
#define E7_ROW      0x222222u
#define E7_TITLE    0x2b2b2bu
#define E7_TEXT     0xffffffu
#define E7_SEL      0xfbd624u
#define E7_GREEN    0x587000u
#define E7_VALUE    0xbebebeu
#define E7_BAR      0xa0a0a0u
#define E7_TABS     7
#define E7_ROWS     7

typedef struct {
    uint8_t *out;
    size_t stride;
    unsigned w;
    unsigned h;
} E7View;

static void e7_fill(E7View *v, unsigned x, unsigned y, unsigned w, unsigned h,
                    uint32_t rgb, unsigned alpha)
{
    if (x >= v->w || y >= v->h || !w || !h) {
        return;
    }
    w = MIN(w, v->w - x);
    h = MIN(h, v->h - y);
    for (unsigned row = 0; row < h; row++) {
        uint8_t *line = v->out + (size_t)(y + row) * v->stride;
        for (unsigned col = 0; col < w; col++) {
            uint8_t *px = line + (size_t)(x + col) * 4;
            uint32_t old = ldl_p(px);
            unsigned red = (((rgb >> 16) & 0xff) * alpha +
                            ((old >> 16) & 0xff) * (255 - alpha)) / 255;
            unsigned green = (((rgb >> 8) & 0xff) * alpha +
                              ((old >> 8) & 0xff) * (255 - alpha)) / 255;
            unsigned blue = ((rgb & 0xff) * alpha + (old & 0xff) *
                             (255 - alpha)) / 255;

            stl_p(px, (red << 16) | (green << 8) | blue);
        }
    }
}

static void e7_slash(E7View *v, int x0, int y0, int x1, int y1, int thick,
                     uint32_t rgb)
{
    int steps = MAX(MAX(abs(x1 - x0), abs(y1 - y0)), 1);

    for (int i = 0; i <= steps; i++) {
        int x = x0 + (x1 - x0) * i / steps;
        int y = y0 + (y1 - y0) * i / steps;

        if (x >= 0 && y >= 0) {
            e7_fill(v, x, y, thick, thick, rgb, 255);
        }
    }
}

static unsigned e7_corner(unsigned r, unsigned dy)
{
    unsigned dx = 0;

    while (dx + 1 <= r && (dx + 1) * (dx + 1) + dy * dy <= r * r) {
        dx++;
    }
    return r - dx;
}

/* Every bar, chip and box on the Z 8 screen has the same corner radius, so
 * the whole skin is built from this one primitive. */
static void e7_rrect(E7View *v, unsigned x, unsigned y, unsigned w, unsigned h,
                     unsigned r, uint32_t rgb, unsigned alpha)
{
    r = MIN(r, MIN(w, h) / 2);
    for (unsigned row = 0; row < h; row++) {
        unsigned dy = row < r ? r - 1 - row : (row >= h - r ? row - (h - r) : 0);
        unsigned in = e7_corner(r, dy);

        e7_fill(v, x + in, y + row, w - 2 * in, 1, rgb, alpha);
    }
}

static void e7_ring(E7View *v, int cx, int cy, int r, int thick, uint32_t rgb)
{
    for (int dy = -r; dy <= r; dy++) {
        int dx = 0;

        while (dx + 1 <= r && (dx + 1) * (dx + 1) + dy * dy <= r * r) {
            dx++;
        }
        for (int k = 0; k < thick && dx - k >= 0; k++) {
            e7_fill(v, cx - dx + k, cy + dy, 1, 1, rgb, 255);
            e7_fill(v, cx + dx - k, cy + dy, 1, 1, rgb, 255);
        }
    }
}

static void e7_glyph(E7View *v, unsigned tab, unsigned x, unsigned y,
                     unsigned w, unsigned h, uint32_t rgb)
{
    unsigned gx = x + w / 7, gy = y + h / 7;
    unsigned gw = w - 2 * (w / 7), gh = h - 2 * (h / 7);
    unsigned t = MAX(2, gw / 9);

    if (gw < 8 || gh < 8) {
        return;
    }
    switch (tab) {
    case 0: /* photo shooting menu: body, prism bump, lens */
        e7_rrect(v, gx, gy + gh / 5, gw, gh * 3 / 5, t, rgb, 255);
        e7_fill(v, gx + gw / 3, gy, gw / 3, gh / 5, rgb, 255);
        e7_ring(v, gx + gw / 2, gy + gh * 4 / 10, MIN(gw, gh) / 5, t,
                rgb == E7_TEXT ? E7_GREEN : E7_BG);
        break;
    case 1: /* movie menu: camcorder body, viewfinder bump and lens hood */
        e7_rrect(v, gx, gy + gh / 5, gw * 2 / 3, gh / 2, t, rgb, 255);
        e7_fill(v, gx + gw / 8, gy, gw / 4, gh / 5, rgb, 255);
        for (unsigned i = 0; i < gh * 2 / 5; i++) {
            unsigned span = gw / 3 * (1 - 2 * ABS((int)i - (int)gh / 5)
                                      / MAX(gh * 2 / 5, 1));

            e7_fill(v, gx + gw * 2 / 3, gy + gh / 5 + i, MAX(span, 1), 1,
                    rgb, 255);
        }
        break;
    case 2: /* retouch menu: pencil */
        e7_slash(v, gx, gy + gh - t, gx + gw - 3 * t, gy + 2 * t, t, rgb);
        e7_fill(v, gx + gw - 3 * t, gy, 3 * t, 2 * t, rgb, 255);
        break;
    case 3: /* playback: triangle inside a rounded frame */
        e7_rrect(v, gx, gy, gw, gh, t, rgb, 255);
        e7_rrect(v, gx + t, gy + t, gw - 2 * t, gh - 2 * t, t,
                 rgb == E7_TEXT ? E7_GREEN : E7_BG, 255);
        for (unsigned i = 0; i < gh / 2; i++) {
            unsigned span = gw / 3 * (gh / 2 - 2 * ABS((int)i - (int)gh / 4))
                            / MAX(gh / 2, 1);
            e7_fill(v, gx + gw / 3, gy + gh / 4 + i, MAX(span, 1), 1, rgb, 255);
        }
        break;
    case 4: /* setup menu: wrench, notched ring head on a handle */
        e7_ring(v, gx + gw / 4, gy + gh / 4, MIN(gw, gh) / 4, t, rgb);
        e7_fill(v, gx, gy, gw / 4 + t, gh / 4,
                rgb == E7_TEXT ? E7_GREEN : E7_BG, 255);
        e7_slash(v, gx + gw / 3, gy + gh / 3, gx + gw - t, gy + gh - t, t, rgb);
        break;
    case 5: /* network menu: globe, outer ring with equator and meridian */
        e7_ring(v, gx + gw / 2, gy + gh / 2, MIN(gw, gh) / 2 - t, t, rgb);
        e7_fill(v, gx + gw / 8, gy + gh / 2 - t / 2, gw * 3 / 4, t, rgb, 255);
        e7_ring(v, gx + gw / 2, gy + gh / 2, MIN(gw, gh) / 4, MAX(1, t / 2),
                rgb);
        break;
    default: /* my menu / recent: three lines with a marker */
        for (unsigned i = 0; i < 3; i++) {
            unsigned span = i == 1 ? gw / 2 : gw;

            e7_fill(v, gx, gy + i * gh / 3, span, t, rgb, 255);
        }
        e7_fill(v, gx + gw - t, gy + gh / 3, t, gh / 3, rgb, 255);
        break;
    }
}

static void e7_skin(Z62LCD *s, E7View *v)
{
    /* Geometry measured off the render: tab cells 91x91 from x=6 and
     * vertically centred, title bar y 0..90, rows 82 px tall spanning
     * x 108..1006, scrollbar against the right edge. */
    unsigned col_x = v->w * 6 / 1024;
    unsigned cell = MAX(24, v->h * 91 / 682);
    unsigned col_w = MIN(MAX(24, v->w * 91 / 1024), cell);
    unsigned col_top = (v->h - cell * E7_TABS) / 2;
    unsigned list_x = v->w * 108 / 1024;
    unsigned list_w = MAX(24, v->w * 1006 / 1024 - list_x);
    unsigned title_h = v->h * 90 / 682;
    unsigned row_h = MAX(16, v->h * 82 / 682);
    unsigned live = s->e7_tab % E7_TABS;
    unsigned mark = s->e7_row % E7_ROWS;
    unsigned cells = 0;

    /* The Z 8 menu screen is opaque: the render shows pure black behind the
     * panel, not the live view dimmed. */
    e7_fill(v, 0, 0, v->w, v->h, E7_BG, 255);
    for (unsigned tab = 0; tab < E7_TABS; tab++) {
        bool on = tab == live;
        unsigned cx = col_x, cy = col_top + tab * cell;

        if (on) {
            e7_rrect(v, cx, cy, col_w, cell - cell / 12, cell / 11,
                     E7_GREEN, 255);
        }
        e7_glyph(v, tab, cx, cy, col_w, cell - cell / 12,
                 on ? E7_TEXT : E7_BAR);
        cells++;
    }
    /* title bar: flat and full width, with a placeholder for the heading */
    e7_fill(v, list_x, 0, list_w, title_h, E7_TITLE, 255);
    e7_fill(v, list_x + list_w * 33 / 100, title_h / 2 - title_h / 8,
            list_w * 34 / 100, title_h / 4, E7_TEXT, 255);
    cells++;
    for (unsigned i = 0; i < E7_ROWS; i++) {
        bool on = i == mark;
        unsigned y = title_h + i * row_h;
        unsigned pad = row_h / 4;
        unsigned cw = MAX(4, row_h / 4);
        unsigned value = MAX(8, list_w * 12 / 100);
        unsigned cx = list_x + list_w - pad - cw / 2;
        unsigned vx = cx - value - cw;
        unsigned label = MIN(list_w * 34 / 100,
                             MAX(8, (vx > list_x + pad + 16 ? vx - pad
                                                            : list_x + pad)
                                  - list_x - pad));

        if (y >= v->h) {
            break;
        }
        e7_fill(v, list_x, y, list_w, MIN(row_h, v->h - y),
                on ? E7_SEL : E7_ROW, 255);
        /* the render separates rows with a one pixel black rule */
        e7_fill(v, list_x, y + row_h - 1, list_w, 1, E7_BG, 255);
        /* label and value placeholders: the Z 8 row shape without inventing
         * any copy this firmware did not draw */
        e7_fill(v, list_x + pad, y + row_h / 2 - row_h / 8, label, row_h / 4,
                on ? E7_BG : E7_TEXT, 255);
        if (vx > list_x + pad + label) {
            e7_fill(v, vx, y + row_h / 2 - row_h / 6, value, row_h / 3,
                    on ? E7_BG : E7_VALUE, 255);
        }
        e7_slash(v, cx - cw / 2, y + row_h / 3, cx, y + row_h / 2,
                 MAX(1, row_h / 14), on ? E7_BG : E7_VALUE);
        e7_slash(v, cx, y + row_h / 2, cx - cw / 2, y + row_h * 2 / 3,
                 MAX(1, row_h / 14), on ? E7_BG : E7_VALUE);
        cells++;
    }
    e7_fill(v, v->w - MAX(3, v->w * 9 / 1024), title_h + mark * row_h,
            MAX(3, v->w * 9 / 1024), row_h * 2, E7_VALUE, 255);
    s->e7_cells = cells;
}

static void lcd_present(void *opaque)
{
    Z62LCD *s = opaque;
    uint32_t *r = s->regs + 0x4000 / 4;
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              (int64_t)MAX(s->frame_ms, 1u) * 1000000);
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
    bool reverse = false;
    bool turn = att_turn(s->attitude, w, h, &reverse);
    unsigned out_w = turn ? h : w, out_h = turn ? w : h;
    DisplaySurface *surface = qemu_console_surface(s->console);
    if (surface_width(surface) != out_w || surface_height(surface) != out_h) {
        qemu_console_resize(s->console, out_w, out_h);
        surface = qemu_console_surface(s->console);
    }
    unsigned stride = surface_stride(surface);
    uint8_t *out = surface_data(surface);
    /* Compose at the geometry the guest programmed, then commit it to the
     * scan-out turned or not; the video and overlay paths keep working on
     * canvas coordinates either way. */
    size_t pitch = (size_t)w * 4;
    g_autofree uint32_t *canvas32 = g_malloc0((size_t)w * h * sizeof(uint32_t));
    uint8_t *canvas = (uint8_t *)canvas32;
    memset(out, 0, (size_t)stride * out_h);
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
    s->video_y = video ? ya : 0;
    s->video_uv = video ? ua : 0;
    s->video_format = r[0x200 / 4];
    s->video_w = w;
    s->video_h = h;
    if (video && s->live_view_fill && s->sensor_sim && !s->live_view_filled) {
        if (z62_sensor_sim_fill_yuv(s->sensor_sim, ya, ua, w, h, ys, us)) {
            s->live_view_filled = true;
            fprintf(stderr, "Z62_LIVEVIEW filled %ux%u y=%08x uv=%08x "
                    "strides=%u/%u\n", w, h, ya, ua, ys, us);
        }
    }
    if (video && getenv("Z62_LCD_VIDEO_TRACE") &&
        (ya != s->trace_ya || ua != s->trace_ua || !s->video_changes)) {
        s->trace_ya = ya;
        s->trace_ua = ua;
        s->video_changes++;
        fprintf(stderr, "Z62_LCD_VIDEO ya=%08x ua=%08x %ux%u stride=%u/%u fmt=%08x nv12=%u\n",
                ya, ua, w, h, ys, us, r[0x200 / 4], nv12);
    }
    if (video) {
        if (s->test_frame) {
            lcd_test_frame(s, ya, ua, ys, us, w, h, nv12, s->test_seq);
        }
        g_autofree uint8_t *yp = g_malloc((size_t)ys * h);
        g_autofree uint8_t *up = g_malloc((size_t)us * uvh);
        nikon_gpu_read(&s->reader, ya, yp, (size_t)ys * h);
        nikon_gpu_read(&s->reader, ua, up, (size_t)us * uvh);
        if (getenv("Z62_LCD_PLANE_TRACE") && s->frames < 4) {
            fprintf(stderr, "Z62_LCD_PLANE y=%02x%02x%02x%02x u=%02x%02x%02x%02x\n",
                    yp[0], yp[1], yp[2], yp[3], up[0], up[1], up[2], up[3]);
        }
        for (unsigned y = 0; y < h; y++) {
            for (unsigned x = 0; x < w; x++) {
                uint8_t pixel[4];
                size_t uv = (size_t)(nv12 ? y / 2 : y) * us + (x & ~1U);
                nikon_yuv_rgb(yp[(size_t)y * ys + x], up[uv], up[uv + 1], pixel);
                stl_p(canvas + (size_t)y * pitch + x * 4,
                      (pixel[0] << 16) | (pixel[1] << 8) | pixel[2]);
            }
        }
    }
    unsigned overlays = lcd_overlay(s, r, 0, canvas, pitch, w, h) +
                        lcd_overlay(s, r, 1, canvas, pitch, w, h);
    if (!turn) {
        for (unsigned y = 0; y < h; y++) {
            memcpy(out + (size_t)y * stride, canvas + (size_t)y * pitch,
                   (size_t)w * 4);
        }
    } else if (!reverse) {
        for (unsigned y = 0; y < h; y++) {
            for (unsigned x = 0; x < w; x++) {
                stl_p(out + (size_t)x * stride + (size_t)(h - 1 - y) * 4,
                      ldl_p(canvas + (size_t)y * pitch + (size_t)x * 4));
            }
        }
    } else {
        for (unsigned y = 0; y < h; y++) {
            for (unsigned x = 0; x < w; x++) {
                stl_p(out + (size_t)(w - 1 - x) * stride + (size_t)y * 4,
                      ldl_p(canvas + (size_t)y * pitch + (size_t)x * 4));
            }
        }
    }
    /* Chrome goes on last: the commit above writes every scan-out pixel, so
     * drawing the skin before it would simply be painted over. */
    if (s->e7_ui && out_w >= 320 && out_h >= 240) {
        E7View view = { out, stride, out_w, out_h };
        e7_skin(s, &view);
    } else {
        s->e7_cells = 0;
    }
    s->scanout_w = out_w;
    s->scanout_h = out_h;
    qemu_console_update(s->console, 0, 0, out_w, out_h);
    if (s->attitude != ATT_AUTO || s->e7_ui) {
        g_autofree char *state = g_strdup_printf(
            "%s e7=%s tab=%u row=%u src=%ux%u out=%ux%u stride=%u cells=%u",
            att_names[s->attitude], s->e7_ui ? "on" : "off",
            (unsigned)(s->e7_tab % E7_TABS), (unsigned)(s->e7_row % E7_ROWS),
            w, h, out_w, out_h, stride, s->e7_cells);

        if (g_strcmp0(state, (const char *)s->e7_state)) {
            g_strlcpy((char *)s->e7_state, state, sizeof(s->e7_state));
            fprintf(stderr, "Z62_LCD_UI %s\n", state);
        }
    }
    if ((video || overlays) && s->frames++ == 0) {
        fprintf(stderr, "Z62_LCD_SCANOUT size=%ux%u bank=%u video=%u overlays=%u\n",
                out_w, out_h, bank, video, overlays);
    }
}

static void lcd_reset(Object *obj, ResetType type)
{
    Z62LCD *s = Z62_LCD(obj);
    memset(s->regs, 0, sizeof(s->regs));
    s->frames = 0;
    s->test_seq = 0;
    s->video_y = s->video_uv = s->video_format = 0;
    s->video_w = s->video_h = 0;
    s->trace_ya = s->trace_ua = 0;
    s->video_changes = 0;
    lcd_irqs(s);
    DisplaySurface *surface = qemu_console_surface(s->console);
    memset(surface_data(surface), 0,
           (size_t)surface_stride(surface) * surface_height(surface));
    qemu_console_update(s->console, 0, 0, surface_width(surface), surface_height(surface));
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 60000000);
}

static char *lcd_get_attitude(Object *obj, Error **errp)
{
    return g_strdup(att_names[Z62_LCD(obj)->attitude]);
}

static void lcd_set_attitude(Object *obj, const char *value, Error **errp)
{
    int mode = att_parse(value);

    if (mode < 0) {
        error_setg(errp, "unknown attitude '%s' (auto, landscape, portrait, "
                         "reverse-portrait)", value);
        return;
    }
    Z62_LCD(obj)->attitude = mode;
}

static bool lcd_get_e7(Object *obj, Error **errp)
{
    return Z62_LCD(obj)->e7_ui;
}

static void lcd_set_e7(Object *obj, bool value, Error **errp)
{
    Z62_LCD(obj)->e7_ui = value;
}

static void lcd_realize(DeviceState *dev, Error **errp)
{
    Z62LCD *s = Z62_LCD(dev);
    const char *attitude = getenv("Z62_LCD_ATTITUDE");
    const char *skin = getenv("Z62_LCD_E7");

    if (attitude) {
        int mode = att_parse(attitude);

        if (mode >= 0) {
            s->attitude = mode;
        }
    }
    if (skin) {
        s->e7_ui = *skin == '1' || !g_ascii_strcasecmp(skin, "on") ||
                   !g_ascii_strcasecmp(skin, "yes");
    }
    if (!s->root) {
        error_setg(errp, "Z62 LCD requires the master engine memory view");
        return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z62_LCD);
    s->reader.memory = &s->memory;
    if (getenv("Z62_LCD_TEST_FRAME")) {
        s->test_frame = true;
    }
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
    s->e7_row = 3;
    /* Runtime switches, so the lab can turn the panel without a reboot:
     * qmp qom-set /machine/z62-lcd attitude portrait (e7-ui, e7-tab alike).
     */
    object_property_add_str(obj, "attitude", lcd_get_attitude,
                            lcd_set_attitude);
    object_property_add_bool(obj, "e7-ui", lcd_get_e7, lcd_set_e7);
    object_property_add_uint8_ptr(obj, "e7-tab", &s->e7_tab,
                                  OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint8_ptr(obj, "e7-row", &s->e7_row,
                                  OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "e7-cells", &s->e7_cells,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "scanout-width", &s->scanout_w,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "scanout-height", &s->scanout_h,
                                   OBJ_PROP_FLAG_READ);
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

static int lcd_post_load(void *opaque, int version_id)
{
    Z62LCD *s = opaque;
    lcd_irqs(s);
    /* Repaint immediately from the restored scan-out registers and guest RAM;
     * the periodic timer alone would leave a black console until it fires. */
    lcd_present(s);
    return 0;
}

static const VMStateDescription lcd_vmstate = {
    .name = TYPE_Z62_LCD,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = lcd_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, Z62LCD, LCD_BYTES / 4),
        VMSTATE_UINT32(frames, Z62LCD),
        VMSTATE_BOOL(test_frame, Z62LCD),
        VMSTATE_UINT64(test_seq, Z62LCD),
        VMSTATE_UINT32(video_y, Z62LCD),
        VMSTATE_UINT32(video_uv, Z62LCD),
        VMSTATE_UINT32(video_format, Z62LCD),
        VMSTATE_UINT32(video_w, Z62LCD),
        VMSTATE_UINT32(video_h, Z62LCD),
        VMSTATE_UINT32(trace_ya, Z62LCD),
        VMSTATE_UINT32(trace_ua, Z62LCD),
        VMSTATE_UINT32(video_changes, Z62LCD),
        VMSTATE_UINT32(overlay_traces, Z62LCD),
        VMSTATE_INT32(overlay_bank, Z62LCD),
        VMSTATE_INT32(attitude, Z62LCD),
        VMSTATE_BOOL(e7_ui, Z62LCD),
        VMSTATE_UINT8(e7_tab, Z62LCD),
        VMSTATE_UINT8(e7_row, Z62LCD),
        VMSTATE_UINT32(e7_cells, Z62LCD),
        VMSTATE_UINT32(scanout_w, Z62LCD),
        VMSTATE_UINT32(scanout_h, Z62LCD),
        VMSTATE_BUFFER(e7_state, Z62LCD),
        VMSTATE_TIMER_PTR(timer, Z62LCD),
        VMSTATE_END_OF_LIST()
    },
};

static void lcd_class_init(ObjectClass *klass, const void *data)
{
    static const Property properties[] = {
        DEFINE_PROP_LINK("root", Z62LCD, root, TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_UINT32("scanned-frames", Z62LCD, frames, 0),
        DEFINE_PROP_UINT32("frame-ms", Z62LCD, frame_ms, 60),
        DEFINE_PROP_LINK("sensor-sim", Z62LCD, sensor_sim, TYPE_Z62_SENSOR_SIM,
                         Z62SensorSim *),
        DEFINE_PROP_BOOL("live-view-fill", Z62LCD, live_view_fill, false),
        DEFINE_PROP_BOOL("test-frame", Z62LCD, test_frame, false),
        DEFINE_PROP_INT32("overlay-bank", Z62LCD, overlay_bank, -1),
        DEFINE_PROP_UINT32("video-y", Z62LCD, video_y, 0),
        DEFINE_PROP_UINT32("video-uv", Z62LCD, video_uv, 0),
        DEFINE_PROP_UINT32("video-format", Z62LCD, video_format, 0),
        DEFINE_PROP_UINT32("video-width", Z62LCD, video_w, 0),
        DEFINE_PROP_UINT32("video-height", Z62LCD, video_h, 0),
    };
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = lcd_realize;
    dc->unrealize = lcd_unrealize;
    dc->vmsd = &lcd_vmstate;
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
