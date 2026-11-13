/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional IMX410-style frame source for the Z6 II laboratory board.
 * The pixel count, pitch and exposure range follow public IMX410/Z 6 II
 * facts. The rolling-shutter readout, the synthetic scene and the exposure
 * response are explicit functional models, not measured silicon timing or
 * recovered image data. */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include <math.h>
#include "nikon-z62-sensor-sim.h"

#define SENSOR_BARW 28
#define SENSOR_BASE_EXPOSURE_NS 16666667.0 /* functional 1/60 s reference */

struct Z62SensorSim {
    SysBusDevice parent_obj;
    MemoryRegion *root;
    AddressSpace memory;
    bool enabled;
    bool electronic_shutter;
    uint64_t exposure_ns;
    uint32_t iso;
    uint64_t readout_ns;
    uint32_t frame_rate;
    uint32_t scene;
    uint32_t motion_speed;
    uint32_t sensor_width, sensor_height, pixel_pitch_nm;
    bool trace;
    uint64_t frames;
    uint64_t write_errors;
    uint32_t last_gain_milli;
    uint64_t last_render_ns;
    uint32_t last_address;
    unsigned w, h;
    uint8_t *cache;
    bool has_frame;
    char *capture_prefix;
    uint32_t capture_width, capture_height;
    /* The capture buffer is 0x2313880 bytes, which is 6048x4024x1.5 - i.e.
     * 12-bit packed, not 16-bit per pixel. Writing 16-bit into it made the
     * first sixth of the buffer plausible and the rest zeros, so a format
     * mismatch looked like "the firmware ignores the frame". */
    bool capture_packed12;
    uint64_t captures;
    /* Continuous live-view source: the sensor keeps producing frames at
     * frame_rate even when the firmware does not ask the GPU to redraw.
     * Only the live-view buffer is touched; composition targets are not. */
    bool stream;
    QEMUTimer *stream_timer;
    uint32_t stream_interval_ns;
    uint32_t stream_address;
    unsigned stream_w, stream_h;
    uint64_t stream_frames;
    /* Read-only body-setting bridge. The native DataService stores Tv/Sv as
     * signed APEX24 integers in the master RAM window; when enabled, the
     * functional sensor consumes those words instead of independent QOM
     * values. Addresses are taken from the stock 1.70 parameter descriptors,
     * not from guessed registers. */
    bool link_firmware_settings;
    uint32_t tv_address, sv_address, av_address;
    int32_t firmware_tv, firmware_sv, firmware_av;
    bool firmware_tv_valid, firmware_sv_valid, firmware_av_valid;
    uint64_t firmware_link_updates;
    uint64_t firmware_link_errors;
};

#define Z62_DSID_TV_APEX24_ADDR 0xc25616f0u
#define Z62_DSID_SV_APEX24_ADDR 0xc25616fcu
#define Z62_DSID_AV_APEX24_ADDR 0xc25616f4u

static const uint8_t scene_bars[8][3] = {
    {235, 235, 235}, {235, 235, 16}, {16, 235, 235}, {16, 235, 16},
    {235, 16, 235}, {235, 16, 16}, {16, 16, 235}, {16, 16, 16},
};

static uint32_t sensor_hash(uint32_t x, uint32_t y, uint64_t frame)
{
    uint32_t v = x * 0x9e3779b1u ^ y * 0x85ebca6bu ^
                 (uint32_t)frame * 0xc2b2ae35u;
    v ^= v >> 16;
    v *= 0x7feb352du;
    v ^= v >> 15;
    return v;
}

/* APEX24 -> physical units used by the image model. The nominal firmware
 * tables are rounded; these exponentials are the functional approximation,
 * not a claim about the exact camera lookup table. */
static uint64_t sensor_tv_ns(int32_t tv)
{
    double seconds = exp2(-(double)tv / 24.0);
    if (!isfinite(seconds) || seconds <= 0.0) {
        return 0;
    }
    return (uint64_t)llrint(CLAMP(seconds * 1e9, 100000.0, 3e10));
}

static uint32_t sensor_sv_iso(int32_t sv)
{
    double iso = 3.125 * exp2((double)sv / 24.0);
    if (!isfinite(iso)) {
        return 0;
    }
    return (uint32_t)llrint(CLAMP(iso, 50.0, 102400.0));
}

/* Two 4-byte reads per frame. Failure leaves the previous value untouched and
 * counts one error per transition; it never aborts the frame. */
static void sensor_sync_firmware_settings(Z62SensorSim *s)
{
    uint32_t word;
    MemTxResult result;

    if (!s->link_firmware_settings || !s->root) {
        return;
    }
    bool updated = false;
    result = address_space_read(&s->memory, s->tv_address, MEMTXATTRS_UNSPECIFIED,
                               &word, sizeof(word));
    if (result == MEMTX_OK) {
        uint64_t ns = sensor_tv_ns((int32_t)ldl_le_p(&word));
        if (ns) {
            s->firmware_tv = (int32_t)ldl_le_p(&word);
            s->firmware_tv_valid = true;
            if (s->exposure_ns != ns) {
                s->exposure_ns = ns;
                updated = true;
            }
        }
    } else if (s->firmware_link_errors != UINT64_MAX) {
        s->firmware_link_errors++;
    }
    result = address_space_read(&s->memory, s->sv_address, MEMTXATTRS_UNSPECIFIED,
                               &word, sizeof(word));
    if (result == MEMTX_OK) {
        uint32_t iso = sensor_sv_iso((int32_t)ldl_le_p(&word));
        if (iso) {
            s->firmware_sv = (int32_t)ldl_le_p(&word);
            s->firmware_sv_valid = true;
            if (s->iso != iso) {
                s->iso = iso;
                updated = true;
            }
        }
    } else if (s->firmware_link_errors != UINT64_MAX) {
        s->firmware_link_errors++;
    }
    result = address_space_read(&s->memory, s->av_address, MEMTXATTRS_UNSPECIFIED,
                               &word, sizeof(word));
    if (result == MEMTX_OK) {
        int32_t av = (int32_t)ldl_le_p(&word);
        /* Accept the camera's lens range; reject a torn or unmapped word. */
        if (av >= -96 && av <= 192) {
            s->firmware_av = av;
            s->firmware_av_valid = true;
        }
    } else if (s->firmware_link_errors != UINT64_MAX) {
        s->firmware_link_errors++;
    }
    if (updated && s->firmware_link_updates != UINT64_MAX) {
        s->firmware_link_updates++;
    }
}

static double sensor_gain(const Z62SensorSim *s)
{
    double exposure = s->exposure_ns ? s->exposure_ns : 1;
    double gain = exposure / SENSOR_BASE_EXPOSURE_NS * (s->iso / 100.0);
    if (s->link_firmware_settings && s->firmware_av_valid) {
        /* APEX Av24 = 48*log2(N). Relative to the boot default Av24=24
         * (f/1.4), illumination scales as (N_ref/N)^2. This is a functional
         * lens-aperture model, not a measured transmission curve. */
        double f = exp2((double)s->firmware_av / 48.0);
        double ref = exp2(24.0 / 48.0);
        if (isfinite(f) && f > 0.0) {
            gain *= (ref / f) * (ref / f);
        }
    }
    return CLAMP(gain, 0.002, 64.0);
}

static double sensor_bar_pos(double t, unsigned w, uint32_t speed)
{
    double span = w + SENSOR_BARW * 2;
    double p = fmod(40.0 + speed * t, span);
    if (p < 0) {
        p += span;
    }
    return p - SENSOR_BARW;
}

static void sensor_scene_pixel(const Z62SensorSim *s, unsigned x, unsigned y,
                               unsigned w, unsigned h, double t0,
                               double exposure_s, uint32_t iso,
                               uint32_t motion, uint8_t rgb[3])
{
    unsigned bar = MIN(x * 8 / MAX(w, 1), 7);
    double base[3] = { scene_bars[bar][0], scene_bars[bar][1],
                       scene_bars[bar][2] };
    if (s->scene & 1) {
        unsigned gray = w > 1 ? x * 255 / (w - 1) : 128;
        base[0] = base[1] = base[2] = gray;
        if (!(y % 64) || !(x % 64)) {
            base[0] = base[1] = base[2] = 220;
        }
    }
    double t_row = s->electronic_shutter && h ?
        t0 + (double)y / h * (CLAMP(s->readout_ns, 100000, 1000000000ULL) / 1e9) : t0;
    unsigned hits = 0;
    for (unsigned k = 0; k < 4; k++) {
        double tk = t_row + exposure_s * (k + 0.5) / 4.0;
        double bx = sensor_bar_pos(tk, w, motion);
        if (x + 0.5 >= bx && x + 0.5 < bx + SENSOR_BARW) {
            hits++;
        }
    }
    double cover = hits / 4.0;
    double gain = sensor_gain(s);
    double noise_amp = 0.7 + 1.2 * sqrt(iso / 100.0);
    int noise = (int)(sensor_hash(x, y, s->frames) % 31) - 15;
    for (unsigned c = 0; c < 3; c++) {
        double value = base[c] * (1.0 - cover) + 240.0 * cover;
        value = value * gain + noise * noise_amp / 15.0;
        rgb[c] = CLAMP((int)lrint(value), 0, 255);
    }
}

static void sensor_render(Z62SensorSim *s, uint8_t *rgba, unsigned w,
                          unsigned h, int64_t now_ns)
{
    double t0 = now_ns / 1e9;
    double exposure_s = CLAMP(s->exposure_ns, 100000, 30000000000ULL) / 1e9;
    uint32_t iso = CLAMP(s->iso, 50, 102400);
    uint32_t motion = MIN(s->motion_speed, 4000);
    for (unsigned y = 0; y < h; y++) {
        uint8_t *row = rgba + (size_t)y * w * 4;
        for (unsigned x = 0; x < w; x++) {
            uint8_t rgb[3];
            sensor_scene_pixel(s, x, y, w, h, t0, exposure_s, iso, motion, rgb);
            row[x * 4 + 0] = rgb[0];
            row[x * 4 + 1] = rgb[1];
            row[x * 4 + 2] = rgb[2];
            row[x * 4 + 3] = 255;
        }
    }
    s->last_gain_milli = (uint32_t)lrint(sensor_gain(s) * 1000.0);
    s->last_render_ns = now_ns;
    s->frames++;
    if (s->frames <= 4 || s->trace) {
        fprintf(stderr, "Z62_SENSOR_SIM frame=%" PRIu64 " %ux%u exposure_ns=%" PRIu64
                " iso=%u electronic=%u readout_ns=%" PRIu64
                " gain_milli=%u scene=%u motion=%u\n",
                s->frames, w, h, s->exposure_ns, s->iso,
                s->electronic_shutter, s->readout_ns, s->last_gain_milli,
                s->scene & 1, s->motion_speed);
    }
}

bool z62_sensor_sim_frame(Z62SensorSim *s, uint32_t guest_address,
                          uint8_t *host_rgba, unsigned w, unsigned h,
                          int64_t now_ns)
{
    if (!s || !s->enabled || !host_rgba || !w || !h || w > 4096 || h > 4096) {
        return false;
    }
    size_t bytes = (size_t)w * h * 4;
    if (guest_address) {
        s->stream_address = guest_address;
        s->stream_w = w;
        s->stream_h = h;
    }
    sensor_sync_firmware_settings(s);
    bool period_elapsed = !s->has_frame || w != s->w || h != s->h ||
        guest_address != s->last_address || now_ns < s->last_render_ns ||
        now_ns - s->last_render_ns >= 1000000000ULL / MAX(s->frame_rate, 1);
    if (period_elapsed) {
        g_free(s->cache);
        s->cache = g_malloc(bytes);
        s->w = w;
        s->h = h;
        s->last_address = guest_address;
        sensor_render(s, s->cache, w, h, now_ns);
        s->has_frame = true;
        if (guest_address && s->root) {
            MemTxResult result = address_space_write(&s->memory, guest_address,
                MEMTXATTRS_UNSPECIFIED, s->cache, bytes);
            if (result != MEMTX_OK) {
                if (!s->write_errors) {
                    fprintf(stderr, "Z62_SENSOR_SIM write failed address=%08x bytes=%zu\n",
                            guest_address, bytes);
                }
                s->write_errors++;
            }
        }
    }
    memcpy(host_rgba, s->cache, bytes);
    return true;
}

/* Minimal DNG/TIFF writer for synthetic laboratory stills, not camera RAW.
 * Samples are 8-bit scene RGB mapped into a 16-bit RGGB CFA container by
 * multiplying by 64. The identity colour matrix is NOT a colour calibration. */
static bool sensor_capture_error(const char *stage, const char *path, int err)
{
    fprintf(stderr, "Z62_SENSOR_CAPTURE failed stage=%s path=%s errno=%d (%s)\n",
            stage, path ? path : "(none)", err, g_strerror(err));
    return false;
}

static bool sensor_capture_write(int fd, const char *path,
                                 const uint8_t *buf, size_t bytes)
{
    while (bytes) {
        ssize_t written = write(fd, buf, MIN(bytes, INT_MAX));
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return sensor_capture_error("write", path,
                                        written < 0 ? errno : EIO);
        }
        buf += written;
        bytes -= written;
    }
    return true;
}

/* Always close every reserved file, even after an earlier write/close error.
 * Failed captures retain their partial files for inspection; never unlink. */
static bool sensor_capture_close(int fds[3], char *const paths[3])
{
    bool ok = true;

    for (unsigned i = 0; i < 3; i++) {
        if (fds[i] >= 0) {
            if (close(fds[i]) < 0) {
                sensor_capture_error("close", paths[i], errno);
                ok = false;
            }
            fds[i] = -1;
        }
    }
    return ok;
}

typedef struct DngTag {
    uint16_t tag, type;
    uint32_t count;
    const void *data;      /* inline payload for <=4 bytes, else extra area */
    uint32_t inline_value;
} DngTag;

enum {
    TIFF_BYTE = 1, TIFF_ASCII = 2, TIFF_SHORT = 3, TIFF_LONG = 4,
    TIFF_RATIONAL = 5, TIFF_SRATIONAL = 10,
};

static void dng_put16(uint8_t *p, uint16_t v) { stw_le_p(p, v); }
static void dng_put32(uint8_t *p, uint32_t v) { stl_le_p(p, v); }

static size_t dng_tag_bytes(const DngTag *t)
{
    switch (t->type) {
    case TIFF_BYTE: case TIFF_ASCII:
        return t->count;
    case TIFF_SHORT:
        return (size_t)t->count * 2;
    case TIFF_LONG:
        return (size_t)t->count * 4;
    case TIFF_RATIONAL: case TIFF_SRATIONAL:
        return (size_t)t->count * 8;
    default:
        g_assert_not_reached();
    }
}

/* Encode numeric payloads by TIFF type, including inline SHORT arrays and
 * both words of each (signed) rational. Host byte order must never leak. */
static void dng_put_payload(uint8_t *dst, const DngTag *t)
{
    switch (t->type) {
    case TIFF_BYTE: case TIFF_ASCII:
        memcpy(dst, t->data, t->count);
        break;
    case TIFF_SHORT:
        for (size_t i = 0; i < t->count; i++) {
            dng_put16(dst + i * 2, ((const uint16_t *)t->data)[i]);
        }
        break;
    case TIFF_LONG: case TIFF_RATIONAL:
        for (size_t i = 0; i < dng_tag_bytes(t) / 4; i++) {
            dng_put32(dst + i * 4, ((const uint32_t *)t->data)[i]);
        }
        break;
    case TIFF_SRATIONAL:
        for (size_t i = 0; i < (size_t)t->count * 2; i++) {
            dng_put32(dst + i * 4, (uint32_t)((const int32_t *)t->data)[i]);
        }
        break;
    default:
        g_assert_not_reached();
    }
}

static bool sensor_write_dng(int fd, const char *path, const uint16_t *raw,
                             unsigned w, unsigned h, uint32_t iso,
                             uint64_t exposure_ns, uint64_t index)
{
    char model[64];
    snprintf(model, sizeof(model), "Nikon Z 6II QEMU lab #%" PRIu64, index);
    const char *make = "Nikon";
    const char *unique = "Nikon Z 6II (QEMU laboratory sensor)";
    const char *description = "Synthetic 8-bit RGB mapped to 16-bit RGGB CFA "
                              "(RGB8 * 64); identity ColorMatrix1; "
                              "not camera RAW or calibrated color.";
    uint8_t cfacolor[3] = {0, 1, 2};
    uint8_t cfapat[4] = {0, 1, 1, 2};        /* RGGB */
    uint16_t cfarep[2] = {2, 2};
    uint8_t dngver[4] = {1, 4, 0, 0};
    uint8_t dngback[4] = {1, 1, 0, 0};
    int32_t colmat[18] = {1,1, 0,1, 0,1, 0,1, 1,1, 0,1, 0,1, 0,1, 1,1};
    uint32_t neutral[6] = {1,1, 1,1, 1,1};
    /* The effective exposure is 0.1 ms .. 30 s. Nearest microsecond keeps
     * both unsigned TIFF RATIONAL words in range, with <= 500 ns error.
     * Rendering still uses the unrounded, clamped nanosecond exposure. */
    uint32_t expo[2] = {
        (uint32_t)((CLAMP(exposure_ns, 100000, 30000000000ULL) + 500) / 1000),
        1000000u,
    };
    int32_t baseline[2] = {0, 1};
    uint16_t black = 0, white = 16383, iso16 = (uint16_t)CLAMP(iso, 1, 65535);
    uint16_t sample_format = 1, orient = 1;

    DngTag tags[32];
    unsigned n = 0;
#define TAG(t, ty, c, d, iv) \
    do { tags[n++] = (DngTag){t, ty, c, (const void *)(d), iv}; } while (0)
    TAG(256, TIFF_LONG, 1, NULL, w);
    TAG(257, TIFF_LONG, 1, NULL, h);
    TAG(258, TIFF_SHORT, 1, NULL, 16);
    TAG(259, TIFF_SHORT, 1, NULL, 1);
    TAG(262, TIFF_SHORT, 1, NULL, 32803);
    TAG(270, TIFF_ASCII, (uint32_t)strlen(description) + 1, description, 0);
    TAG(271, TIFF_ASCII, (uint32_t)strlen(make) + 1, make, 0);
    TAG(272, TIFF_ASCII, (uint32_t)strlen(model) + 1, model, 0);
    TAG(273, TIFF_LONG, 1, NULL, 0);   /* StripOffsets patched below */
    TAG(274, TIFF_SHORT, 1, NULL, orient);
    TAG(277, TIFF_SHORT, 1, NULL, 1);
    TAG(278, TIFF_LONG, 1, NULL, h);
    TAG(279, TIFF_LONG, 1, NULL, (uint32_t)((uint64_t)w * h * 2));
    TAG(284, TIFF_SHORT, 1, NULL, 1);
    TAG(33421, TIFF_SHORT, 2, cfarep, 0);
    TAG(33422, TIFF_BYTE, 4, cfapat, 0);
    TAG(33434, TIFF_RATIONAL, 1, expo, 0);
    TAG(339, TIFF_SHORT, 1, NULL, sample_format);
    TAG(34855, TIFF_SHORT, 1, NULL, iso16);
    TAG(50706, TIFF_BYTE, 4, dngver, 0);
    TAG(50707, TIFF_BYTE, 4, dngback, 0);
    TAG(50708, TIFF_ASCII, (uint32_t)strlen(unique) + 1, unique, 0);
    TAG(50710, TIFF_BYTE, 3, cfacolor, 0);
    TAG(50711, TIFF_SHORT, 1, NULL, 1);
    TAG(50714, TIFF_SHORT, 1, NULL, black);
    TAG(50717, TIFF_LONG, 1, NULL, white);
    TAG(50721, TIFF_SRATIONAL, 9, colmat, 0);
    TAG(50728, TIFF_RATIONAL, 3, neutral, 0);
    TAG(50730, TIFF_SRATIONAL, 1, baseline, 0);
    TAG(50778, TIFF_SHORT, 1, NULL, 21);   /* D65 */
#undef TAG

    /* TIFF-EP requires ascending tag order in the IFD. */
    for (unsigned i = 1; i < n; i++) {
        DngTag key = tags[i];
        int j = (int)i - 1;
        while (j >= 0 && tags[j].tag > key.tag) {
            tags[j + 1] = tags[j];
            j--;
        }
        tags[j + 1] = key;
    }

    uint32_t ifd_off = 8;
    uint32_t ifd_bytes = 2 + 12 * n + 4;
    uint32_t extra_off = ifd_off + ifd_bytes;
    uint32_t extra_len = 0;
    for (unsigned i = 0; i < n; i++) {
        size_t bytes = dng_tag_bytes(&tags[i]);
        if (!tags[i].data) {
            continue;
        }
        if (bytes > 4) {
            extra_len += (uint32_t)((bytes + 3) & ~3u);
        }
    }
    uint32_t pixel_off = extra_off + extra_len;
    uint64_t total = (uint64_t)pixel_off + (uint64_t)w * h * 2;
    uint8_t *buf = g_malloc0(total);
    uint8_t *ifd = buf + ifd_off;
    uint8_t *extra = buf + extra_off;
    dng_put16(buf, 0x4949);          /* little-endian */
    dng_put16(buf + 2, 42);
    dng_put32(buf + 4, ifd_off);
    dng_put16(ifd, (uint16_t)n);
    uint8_t *e = ifd + 2;
    uint32_t extra_cursor = 0;
    for (unsigned i = 0; i < n; i++) {
        DngTag *t = &tags[i];
        size_t bytes = dng_tag_bytes(t);
        dng_put16(e + 0, t->tag);
        dng_put16(e + 2, t->type);
        dng_put32(e + 4, t->count);
        if (t->tag == 273) {
            dng_put32(e + 8, pixel_off);
        } else if (!t->data) {
            if (t->type == TIFF_SHORT) {
                dng_put16(e + 8, (uint16_t)t->inline_value);
                dng_put16(e + 10, 0);
            } else {
                dng_put32(e + 8, t->inline_value);
            }
        } else if (bytes <= 4) {
            dng_put_payload(e + 8, t);
        } else {
            uint32_t off = extra_off + extra_cursor;
            dng_put32(e + 8, off);
            dng_put_payload(extra + extra_cursor, t);
            extra_cursor += (uint32_t)((bytes + 3) & ~3u);
        }
        e += 12;
    }
    dng_put32(e, 0);                  /* no next IFD */
    for (size_t i = 0; i < (size_t)w * h; i++) {
        stw_le_p(buf + pixel_off + i * 2, raw[i]);
    }
    bool ok = sensor_capture_write(fd, path, buf, total);
    g_free(buf);
    return ok;
}

/* File numbers are selected from the output namespace, not the successful
 * capture count. Restart/reset/prefix changes cannot overwrite an old trio.
 * Reserve all three paths exclusively before writing. In a race, any empty
 * reservations are retained and the next number is tried, never truncated. */
static bool sensor_capture_reserve(const char *prefix, uint64_t *index,
                                   char *paths[3], int fds[3])
{
    bool card_mode = g_str_has_suffix(prefix, "/") ||
                     g_str_has_suffix(prefix, "\\");
    g_autofree char *dir = NULL;

    if (card_mode) {
        dir = g_strdup_printf("%sDCIM/100NZ6II", prefix);
        if (g_mkdir_with_parents(dir, 0777) < 0) {
            return sensor_capture_error("mkdir", dir, errno);
        }
    }
    for (*index = 0; *index < UINT64_MAX; (*index)++) {
        bool collision = false;

        for (unsigned i = 0; i < 3; i++) {
            g_clear_pointer(&paths[i], g_free);
        }
        if (card_mode) {
            paths[0] = g_strdup_printf("%s/DSC_%04" PRIu64 ".bayer16",
                                       dir, *index + 1);
            paths[1] = g_strdup_printf("%s/DSC_%04" PRIu64 ".PPM",
                                       dir, *index + 1);
            paths[2] = g_strdup_printf("%s/DSC_%04" PRIu64 ".DNG",
                                       dir, *index + 1);
        } else {
            paths[0] = g_strdup_printf("%s-%" PRIu64 ".bayer16", prefix, *index);
            paths[1] = g_strdup_printf("%s-%" PRIu64 ".ppm", prefix, *index);
            paths[2] = g_strdup_printf("%s-%" PRIu64 ".dng", prefix, *index);
        }
        for (unsigned i = 0; i < 3; i++) {
            if (g_file_test(paths[i], G_FILE_TEST_EXISTS | G_FILE_TEST_IS_SYMLINK)) {
                collision = true;
                break;
            }
        }
        if (collision) {
            continue;
        }
        for (unsigned i = 0; i < 3; i++) {
            fds[i] = g_open(paths[i], O_WRONLY | O_CREAT | O_EXCL | O_BINARY,
                            0666);
            if (fds[i] < 0) {
                if (errno != EEXIST) {
                    return sensor_capture_error("open", paths[i], errno);
                }
                collision = true;
                fprintf(stderr, "Z62_SENSOR_CAPTURE collision path=%s "
                        "index=%" PRIu64 "; reservations retained\n",
                        paths[i], *index);
                break;
            }
        }
        if (!collision) {
            return true;
        }
        if (!sensor_capture_close(fds, paths)) {
            return false;
        }
    }
    return sensor_capture_error("file-number-exhausted", prefix, EOVERFLOW);
}

static uint32_t sensor_raw_at(const uint16_t *raw, int x, int y,
                              unsigned w, unsigned h)
{
    x = CLAMP(x, 0, (int)w - 1);
    y = CLAMP(y, 0, (int)h - 1);
    return raw[(size_t)y * w + x];
}

static void sensor_demosaic(const uint16_t *raw, unsigned w, unsigned h,
                            unsigned x, unsigned y, uint8_t rgb[3])
{
    bool even_y = !(y & 1), even_x = !(x & 1);
    unsigned r, g, b;
    if (even_y && even_x) {
        r = sensor_raw_at(raw, x, y, w, h);
        g = (sensor_raw_at(raw, x - 1, y, w, h) +
             sensor_raw_at(raw, x + 1, y, w, h) +
             sensor_raw_at(raw, x, y - 1, w, h) +
             sensor_raw_at(raw, x, y + 1, w, h) + 2) / 4;
        b = (sensor_raw_at(raw, x - 1, y - 1, w, h) +
             sensor_raw_at(raw, x + 1, y - 1, w, h) +
             sensor_raw_at(raw, x - 1, y + 1, w, h) +
             sensor_raw_at(raw, x + 1, y + 1, w, h) + 2) / 4;
    } else if (!even_y && !even_x) {
        b = sensor_raw_at(raw, x, y, w, h);
        g = (sensor_raw_at(raw, x - 1, y, w, h) +
             sensor_raw_at(raw, x + 1, y, w, h) +
             sensor_raw_at(raw, x, y - 1, w, h) +
             sensor_raw_at(raw, x, y + 1, w, h) + 2) / 4;
        r = (sensor_raw_at(raw, x - 1, y - 1, w, h) +
             sensor_raw_at(raw, x + 1, y - 1, w, h) +
             sensor_raw_at(raw, x - 1, y + 1, w, h) +
             sensor_raw_at(raw, x + 1, y + 1, w, h) + 2) / 4;
    } else {
        g = sensor_raw_at(raw, x, y, w, h);
        if (even_y) {
            r = (sensor_raw_at(raw, x - 1, y, w, h) +
                 sensor_raw_at(raw, x + 1, y, w, h) + 1) / 2;
            b = (sensor_raw_at(raw, x, y - 1, w, h) +
                 sensor_raw_at(raw, x, y + 1, w, h) + 1) / 2;
        } else {
            r = (sensor_raw_at(raw, x, y - 1, w, h) +
                 sensor_raw_at(raw, x, y + 1, w, h) + 1) / 2;
            b = (sensor_raw_at(raw, x - 1, y, w, h) +
                 sensor_raw_at(raw, x + 1, y, w, h) + 1) / 2;
        }
    }
    rgb[0] = CLAMP((int)((r + 32) / 64), 0, 255);
    rgb[1] = CLAMP((int)((g + 32) / 64), 0, 255);
    rgb[2] = CLAMP((int)((b + 32) / 64), 0, 255);
}

bool z62_sensor_sim_capture(Z62SensorSim *s, uint64_t exposure_ns,
                            bool electronic)
{
    if (!s || !s->enabled) {
        return sensor_capture_error("disabled", NULL, ENODEV);
    }
    if (!s->capture_prefix || !s->capture_prefix[0]) {
        return sensor_capture_error("missing-prefix", s->capture_prefix, EINVAL);
    }
    if (s->captures == UINT64_MAX) {
        return sensor_capture_error("capture-count-exhausted",
                                    s->capture_prefix, EOVERFLOW);
    }
    unsigned w = CLAMP(s->capture_width, 64, 8192);
    unsigned h = CLAMP(s->capture_height, 64, 8192);
    uint64_t saved_exposure = s->exposure_ns;
    bool saved_electronic = s->electronic_shutter;
    sensor_sync_firmware_settings(s);
    /* With the body-setting bridge enabled, Tv comes from the firmware's own
     * DataService word. Without it, use the capture request's functional
     * exposure. */
    uint64_t effective_exposure = s->link_firmware_settings && s->firmware_tv_valid
        ? s->exposure_ns : CLAMP(exposure_ns, 100000, 30000000000ULL);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    double t0 = now / 1e9;
    double exposure_s = effective_exposure / 1e9;
    uint32_t iso = CLAMP(s->iso, 50, 102400);
    uint32_t motion = MIN(s->motion_speed, 4000);
    uint64_t index = 0;
    char *paths[3] = {NULL, NULL, NULL};
    int fds[3] = {-1, -1, -1};
    bool ok = false;
    g_autofree uint16_t *raw = NULL;
    g_autofree uint8_t *raw_le = NULL;
    g_autofree uint8_t *ppm = NULL;

    if (!sensor_capture_reserve(s->capture_prefix, &index, paths, fds)) {
        goto out;
    }
    s->exposure_ns = effective_exposure;
    s->electronic_shutter = electronic;
    raw = g_malloc((size_t)w * h * sizeof(uint16_t));
    for (unsigned y = 0; y < h; y++) {
        for (unsigned x = 0; x < w; x++) {
            uint8_t rgb[3];
            sensor_scene_pixel(s, x, y, w, h, t0, exposure_s, iso, motion, rgb);
            unsigned value;
            if (!(y & 1) && !(x & 1)) {
                value = rgb[0];
            } else if ((y & 1) && (x & 1)) {
                value = rgb[2];
            } else {
                value = rgb[1];
            }
            raw[(size_t)y * w + x] = value * 64; /* 0 .. 16320, not 14-bit data */
        }
    }
    raw_le = g_malloc((size_t)w * h * 2);
    for (size_t i = 0; i < (size_t)w * h; i++) {
        stw_le_p(raw_le + i * 2, raw[i]);
    }
    size_t rgb_bytes = (size_t)w * h * 3;
    ppm = g_malloc(64 + rgb_bytes);
    int header = snprintf((char *)ppm, 64, "P6\n%u %u\n255\n", w, h);
    if (header < 0 || header >= 64) {
        sensor_capture_error("ppm-header", paths[1], EIO);
        goto out;
    }
    for (unsigned y = 0; y < h; y++) {
        for (unsigned x = 0; x < w; x++) {
            sensor_demosaic(raw, w, h, x, y,
                            ppm + header + ((size_t)y * w + x) * 3);
        }
    }
    ok = sensor_capture_write(fds[0], paths[0], raw_le, (size_t)w * h * 2) &&
         sensor_capture_write(fds[1], paths[1], ppm, header + rgb_bytes) &&
         sensor_write_dng(fds[2], paths[2], raw, w, h, iso,
                          effective_exposure, index + 1);

out:
    if (!sensor_capture_close(fds, paths)) {
        ok = false;
    }
    if (ok) {
        s->captures++;
        fprintf(stderr, "Z62_SENSOR_CAPTURE success captures=%" PRIu64
                " file_index=%" PRIu64 " %ux%u exposure_ns=%" PRIu64
                " iso=%u electronic=%u gain_milli=%u dng=%s\n",
                s->captures, index, w, h, effective_exposure, iso, electronic,
                (unsigned)lrint(sensor_gain(s) * 1000.0), paths[2]);
    } else {
        fprintf(stderr, "Z62_SENSOR_CAPTURE failed captures=%" PRIu64
                " file_index=%" PRIu64 " prefix=%s; partial files retained\n",
                s->captures, index, s->capture_prefix);
    }
    if (!s->link_firmware_settings) {
        s->exposure_ns = saved_exposure;
    }
    s->electronic_shutter = saved_electronic;
    for (unsigned i = 0; i < 3; i++) {
        g_free(paths[i]);
    }
    return ok;
}

/* Bridge into the firmware's capture path.
 *
 * The scatter record at VMA 10034070 describes PA 80800000 / 0x2313880 as the
 * master engine's physical capture buffer. Until now nothing ever filled it,
 * so every pixel the CaptureService read was zero and no still could be
 * encoded. This writes a real RGGB frame there when the shutter cycle
 * reaches its exposure point. The frame is 16-bit LE samples at the
 * simulation's capture size; the buffer extent would also accommodate a
 * 12-bit packed 6048x4024 sensor readout, which is what the real silicon
 * produces - the packed layout is not modelled yet. */
bool z62_sensor_sim_fill_capture(Z62SensorSim *s, uint32_t guest_address,
                                 uint64_t exposure_ns)
{
    if (!s || !s->enabled || !s->root || !guest_address) {
        return false;
    }
    unsigned w = CLAMP(s->capture_width, 64, 8192);
    unsigned h = CLAMP(s->capture_height, 64, 8192);
    uint64_t saved_exposure = s->exposure_ns;

    sensor_sync_firmware_settings(s);
    uint64_t effective_exposure = s->link_firmware_settings && s->firmware_tv_valid
        ? s->exposure_ns : CLAMP(exposure_ns, 100000, 30000000000ULL);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    double t0 = now / 1e9;
    double exposure_s = effective_exposure / 1e9;
    uint32_t iso = CLAMP(s->iso, 50, 102400);
    uint32_t motion = MIN(s->motion_speed, 4000);

    size_t pixels = (size_t)w * h;
    size_t out_size = s->capture_packed12 ? pixels * 3 / 2 : pixels * 2;
    g_autofree uint8_t *bytes = g_malloc0(out_size);
    for (unsigned y = 0; y < h; y++) {
        for (unsigned x = 0; x < w; x++) {
            uint8_t rgb[3];
            unsigned value;
            sensor_scene_pixel(s, x, y, w, h, t0, exposure_s, iso, motion, rgb);
            if (!(y & 1) && !(x & 1)) {
                value = rgb[0];
            } else if ((y & 1) && (x & 1)) {
                value = rgb[2];
            } else {
                value = rgb[1];
            }
            value = (value * 64) & 0xfff;
            if (s->capture_packed12) {
                /* MIPI RAW12: two pixels in three bytes. */
                unsigned px = x & ~1u;
                uint8_t *dst = bytes + ((size_t)y * w + px) * 3 / 2;
                if (!(x & 1)) {
                    dst[0] = value & 0xff;
                    dst[1] = (dst[1] & 0xf0) | ((value >> 8) & 0x0f);
                } else {
                    dst[1] = ((value & 0x0f) << 4) | (dst[1] & 0x0f);
                    dst[2] = (value >> 4) & 0xff;
                }
            } else {
                stw_le_p(bytes + ((size_t)y * w + x) * 2, value);
            }
        }
    }
    MemTxResult result = address_space_write(&s->memory, guest_address,
                                             MEMTXATTRS_UNSPECIFIED,
                                             bytes, out_size);
    s->exposure_ns = effective_exposure;
    if (!s->link_firmware_settings) {
        s->exposure_ns = saved_exposure;
    }
    if (result != MEMTX_OK) {
        fprintf(stderr, "Z62_CAPTURE_FILL failed address=%08x bytes=%zu\n",
                guest_address, out_size);
        return false;
    }
    fprintf(stderr, "Z62_CAPTURE_FILL address=%08x %ux%u bytes=%zu "
            "format=%s exposure_ns=%" PRIu64 " iso=%u\n",
            guest_address, w, h, out_size,
            s->capture_packed12 ? "raw12" : "raw16", effective_exposure, iso);
    return true;
}

/* Live-view scan-out: render the scene into the firmware's YUV422 planes.
 *
 * With real NOR parameters the body enters its normal live-view path and
 * programs LCD 0x4110=1 (video on), 0x4200=0x300000, Y at 0xdcf33000,
 * UV at 0xdcfdd800, both strides 0x400, geometry 1024x682. Every one of those
 * registers is correct and lcd_present() does take the video branch, but the
 * planes were never written by anything, so the panel stayed black. */
bool z62_sensor_sim_fill_yuv(Z62SensorSim *s, uint32_t y_address,
                             uint32_t uv_address, unsigned w, unsigned h,
                             unsigned y_stride, unsigned uv_stride)
{
    if (!s || !s->enabled || !s->root || !y_address || !uv_address) {
        return false;
    }
    if (w < 2 || h < 2 || w > 4096 || h > 4096 ||
        y_stride < w || uv_stride < w) {
        return false;
    }
    sensor_sync_firmware_settings(s);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    double t0 = now / 1e9;
    double exposure_s = CLAMP(s->exposure_ns, 100000, 30000000000ULL) / 1e9;
    uint32_t iso = CLAMP(s->iso, 50, 102400);
    uint32_t motion = MIN(s->motion_speed, 4000);

    g_autofree uint8_t *ybuf = g_malloc0((size_t)y_stride * h);
    g_autofree uint8_t *uvbuf = g_malloc0((size_t)uv_stride * h);
    for (unsigned y = 0; y < h; y++) {
        for (unsigned x = 0; x + 1 < w; x += 2) {
            uint8_t r0[3], r1[3];
            sensor_scene_pixel(s, x, y, w, h, t0, exposure_s, iso, motion, r0);
            sensor_scene_pixel(s, x + 1, y, w, h, t0, exposure_s, iso, motion,
                               r1);
            /* BT.601 luma, chroma averaged over the pixel pair. */
            ybuf[(size_t)y * y_stride + x] =
                (77u * r0[0] + 150u * r0[1] + 29u * r0[2]) >> 8;
            ybuf[(size_t)y * y_stride + x + 1] =
                (77u * r1[0] + 150u * r1[1] + 29u * r1[2]) >> 8;
            int rr = (r0[0] + r1[0]) >> 1;
            int gg = (r0[1] + r1[1]) >> 1;
            int bb = (r0[2] + r1[2]) >> 1;
            uvbuf[(size_t)y * uv_stride + x] =
                CLAMP(((-43 * rr - 85 * gg + 128 * bb) >> 8) + 128, 0, 255);
            uvbuf[(size_t)y * uv_stride + x + 1] =
                CLAMP(((128 * rr - 107 * gg - 21 * bb) >> 8) + 128, 0, 255);
        }
    }
    MemTxResult ry = address_space_write(&s->memory, y_address,
                                         MEMTXATTRS_UNSPECIFIED, ybuf,
                                         (size_t)y_stride * h);
    MemTxResult ruv = address_space_write(&s->memory, uv_address,
                                          MEMTXATTRS_UNSPECIFIED, uvbuf,
                                          (size_t)uv_stride * h);
    if (ry != MEMTX_OK || ruv != MEMTX_OK) {
        fprintf(stderr, "Z62_LIVEVIEW failed y=%d uv=%d addr=%08x/%08x\n",
                ry, ruv, y_address, uv_address);
        return false;
    }
    return true;
}

static uint32_t sensor_stream_delay(const Z62SensorSim *s)
{
    uint64_t frame_period = (UINT64_C(1000000000) +
                             MAX(s->frame_rate, 1) - 1) / MAX(s->frame_rate, 1);
    /* The timer must not fire just below the frame() period test: at 30 fps
     * 33,333,333 ns is 0.3 ns short, which would starve the stream forever. */
    return (uint32_t)MAX((uint64_t)MAX(s->stream_interval_ns, 1000000u),
                         frame_period + 1);
}

static void sensor_stream_schedule(Z62SensorSim *s)
{
    timer_del(s->stream_timer);
    if (s->stream && s->enabled) {
        timer_mod(s->stream_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   sensor_stream_delay(s));
    }
}

static void sensor_stream_tick(void *opaque)
{
    Z62SensorSim *s = opaque;
    if (!s->stream || !s->enabled) {
        return;
    }
    if (s->stream_address && s->stream_w && s->stream_h) {
        /* Never pass cache: frame() may replace it. Count only a new frame
         * successfully written, not timer firings or cached copies. */
        g_autofree uint8_t *tmp = g_malloc((size_t)s->stream_w * s->stream_h * 4);
        uint64_t before = s->frames, errors = s->write_errors;
        if (z62_sensor_sim_frame(s, s->stream_address, tmp,
                                s->stream_w, s->stream_h,
                                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) &&
            s->frames > before && s->write_errors == errors) {
            s->stream_frames++;
        }
    }
    sensor_stream_schedule(s);
}

static void sensor_sim_reset(Object *obj, ResetType type)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    timer_del(s->stream_timer);
    g_clear_pointer(&s->cache, g_free);
    s->has_frame = false;
    s->frames = s->write_errors = 0;
    s->last_gain_milli = 0;
    s->last_render_ns = 0;
    s->last_address = 0;
    s->w = s->h = 0;
    s->stream_address = s->stream_w = s->stream_h = 0;
    s->stream_frames = 0;
    s->firmware_tv_valid = s->firmware_sv_valid = s->firmware_av_valid = false;
    s->firmware_link_updates = s->firmware_link_errors = 0;
    /* Host capture files do not roll back on reset. Keep capture sequence
     * and successful-capture count so a reset cannot reuse their names. */
}

static void sensor_sim_reset_exit(Object *obj, ResetType type)
{
    sensor_stream_schedule(Z62_SENSOR_SIM(obj));
}

static void sensor_sim_realize(DeviceState *dev, Error **errp)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(dev);
    if (!s->root) {
        error_setg(errp, "Sensor simulation requires the engine memory view");
        return;
    }
    if (!s->capture_prefix && getenv("Z62_SENSOR_CAPTURE")) {
        s->capture_prefix = g_strdup(getenv("Z62_SENSOR_CAPTURE"));
    }
    if (s->exposure_ns < 100000 || s->exposure_ns > 30000000000ULL) {
        error_setg(errp, "exposure-ns must be within 0.1 ms .. 30 s");
        return;
    }
    if (s->readout_ns < 100000 || s->readout_ns > 1000000000ULL) {
        error_setg(errp, "readout-ns must be within 0.1 ms .. 1 s");
        return;
    }
    if (s->iso < 50 || s->iso > 102400) {
        error_setg(errp, "iso must be within 50 .. 102400");
        return;
    }
    if (s->frame_rate < 1 || s->frame_rate > 120) {
        error_setg(errp, "frame-rate must be within 1 .. 120");
        return;
    }
    if (s->motion_speed > 4000 || s->scene > 1) {
        error_setg(errp, "motion-speed or scene out of range");
        return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z62_SENSOR_SIM);
    sensor_stream_schedule(s);
}

static void sensor_sim_unrealize(DeviceState *dev)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(dev);
    timer_del(s->stream_timer);
    address_space_destroy(&s->memory);
    g_clear_pointer(&s->cache, g_free);
    s->has_frame = false;
}

static bool sensor_get_electronic(Object *obj, Error **errp)
{
    return Z62_SENSOR_SIM(obj)->electronic_shutter;
}

static void sensor_set_electronic(Object *obj, bool value, Error **errp)
{
    Z62_SENSOR_SIM(obj)->electronic_shutter = value;
}

static bool sensor_get_enabled(Object *obj, Error **errp)
{
    return Z62_SENSOR_SIM(obj)->enabled;
}

static void sensor_set_enabled(Object *obj, bool value, Error **errp)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    s->enabled = value;
    if (qdev_is_realized(DEVICE(s)) && !device_is_in_reset(DEVICE(s))) {
        sensor_stream_schedule(s);
    }
}

static bool sensor_get_trace(Object *obj, Error **errp)
{
    return Z62_SENSOR_SIM(obj)->trace;
}

static void sensor_set_trace(Object *obj, bool value, Error **errp)
{
    Z62_SENSOR_SIM(obj)->trace = value;
}

static bool sensor_get_stream(Object *obj, Error **errp)
{
    return Z62_SENSOR_SIM(obj)->stream;
}

static void sensor_set_stream(Object *obj, bool value, Error **errp)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    s->stream = value;
    if (qdev_is_realized(DEVICE(s)) && !device_is_in_reset(DEVICE(s))) {
        sensor_stream_schedule(s);
    }
}

static bool sensor_get_stream_active(Object *obj, Error **errp)
{
    return timer_pending(Z62_SENSOR_SIM(obj)->stream_timer);
}

static bool sensor_get_link_valid(Object *obj, Error **errp)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    return s->firmware_tv_valid && s->firmware_sv_valid;
}

static bool sensor_get_av_valid(Object *obj, Error **errp)
{
    return Z62_SENSOR_SIM(obj)->firmware_av_valid;
}

static void sensor_sim_init(Object *obj)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    s->enabled = true;
    s->electronic_shutter = true;
    s->exposure_ns = 16666667;
    s->iso = 100;
    s->readout_ns = 66666667;
    s->frame_rate = 30;
    s->scene = 0;
    s->motion_speed = 600;
    s->sensor_width = 6048;
    s->sensor_height = 4024;
    s->pixel_pitch_nm = 5940;
    s->capture_width = 1920;
    s->capture_height = 1080;
    s->stream = true;
    s->stream_interval_ns = 33333333;
    s->tv_address = Z62_DSID_TV_APEX24_ADDR;
    s->sv_address = Z62_DSID_SV_APEX24_ADDR;
    s->av_address = Z62_DSID_AV_APEX24_ADDR;
    s->stream_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sensor_stream_tick, s);
    object_property_add_bool(obj, "enabled", sensor_get_enabled,
                             sensor_set_enabled);
    object_property_add_bool(obj, "electronic-shutter", sensor_get_electronic,
                             sensor_set_electronic);
    object_property_add_bool(obj, "trace", sensor_get_trace, sensor_set_trace);
    object_property_add_bool(obj, "stream", sensor_get_stream,
                             sensor_set_stream);
    object_property_add_bool(obj, "stream-active", sensor_get_stream_active, NULL);
    object_property_add_uint32_ptr(obj, "stream-address", &s->stream_address,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "firmware-link-updates",
                                   &s->firmware_link_updates, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "firmware-link-errors",
                                   &s->firmware_link_errors, OBJ_PROP_FLAG_READ);
    object_property_add_bool(obj, "firmware-link-valid", sensor_get_link_valid, NULL);
    object_property_add_bool(obj, "firmware-av-valid", sensor_get_av_valid, NULL);
    object_property_add_uint64_ptr(obj, "exposure-ns", &s->exposure_ns,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "readout-ns", &s->readout_ns,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "iso", &s->iso,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "frame-rate", &s->frame_rate,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "scene", &s->scene,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "motion-speed", &s->motion_speed,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "sensor-width", &s->sensor_width,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "sensor-height", &s->sensor_height,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "pixel-pitch-nm", &s->pixel_pitch_nm,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "capture-width", &s->capture_width,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "capture-height", &s->capture_height,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "captures", &s->captures, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "frames", &s->frames, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "write-errors", &s->write_errors,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "last-gain-milli", &s->last_gain_milli,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "stream-frames", &s->stream_frames,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "stream-interval-ns",
                                   &s->stream_interval_ns,
                                   OBJ_PROP_FLAG_READWRITE);
}

static void sensor_sim_finalize(Object *obj)
{
    Z62SensorSim *s = Z62_SENSOR_SIM(obj);
    timer_free(s->stream_timer);
    g_clear_pointer(&s->cache, g_free);
    g_free(s->capture_prefix);
}

/* The rendered cache is host-side and reproducible from the migrated scene
 * parameters; it is discarded on load and the next frame is rendered again.
 * Host capture files are not rolled back by a migration. */
static int sensor_sim_post_load(void *opaque, int version_id)
{
    Z62SensorSim *s = opaque;
    g_clear_pointer(&s->cache, g_free);
    s->has_frame = false;
    s->last_render_ns = 0;
    if (s->stream && s->enabled && !timer_pending(s->stream_timer)) {
        timer_mod(s->stream_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   sensor_stream_delay(s));
    }
    return 0;
}

static const VMStateDescription sensor_sim_vmstate = {
    .name = TYPE_Z62_SENSOR_SIM,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = sensor_sim_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_BOOL(enabled, Z62SensorSim),
        VMSTATE_BOOL(electronic_shutter, Z62SensorSim),
        VMSTATE_UINT64(exposure_ns, Z62SensorSim),
        VMSTATE_UINT32(iso, Z62SensorSim),
        VMSTATE_UINT64(readout_ns, Z62SensorSim),
        VMSTATE_UINT32(frame_rate, Z62SensorSim),
        VMSTATE_UINT32(scene, Z62SensorSim),
        VMSTATE_UINT32(motion_speed, Z62SensorSim),
        VMSTATE_UINT32(sensor_width, Z62SensorSim),
        VMSTATE_UINT32(sensor_height, Z62SensorSim),
        VMSTATE_UINT32(pixel_pitch_nm, Z62SensorSim),
        VMSTATE_BOOL(trace, Z62SensorSim),
        VMSTATE_UINT64(frames, Z62SensorSim),
        VMSTATE_UINT64(write_errors, Z62SensorSim),
        VMSTATE_UINT32(last_gain_milli, Z62SensorSim),
        VMSTATE_UINT64(last_render_ns, Z62SensorSim),
        VMSTATE_UINT32(last_address, Z62SensorSim),
        VMSTATE_UINT32(w, Z62SensorSim),
        VMSTATE_UINT32(h, Z62SensorSim),
        VMSTATE_UINT64(captures, Z62SensorSim),
        VMSTATE_BOOL(stream, Z62SensorSim),
        VMSTATE_UINT32(stream_interval_ns, Z62SensorSim),
        VMSTATE_UINT32(stream_address, Z62SensorSim),
        VMSTATE_UINT32(stream_w, Z62SensorSim),
        VMSTATE_UINT32(stream_h, Z62SensorSim),
        VMSTATE_UINT64(stream_frames, Z62SensorSim),
        VMSTATE_BOOL(link_firmware_settings, Z62SensorSim),
        VMSTATE_UINT32(tv_address, Z62SensorSim),
        VMSTATE_UINT32(sv_address, Z62SensorSim),
        VMSTATE_UINT32(av_address, Z62SensorSim),
        VMSTATE_INT32(firmware_tv, Z62SensorSim),
        VMSTATE_INT32(firmware_sv, Z62SensorSim),
        VMSTATE_INT32(firmware_av, Z62SensorSim),
        VMSTATE_BOOL(firmware_tv_valid, Z62SensorSim),
        VMSTATE_BOOL(firmware_sv_valid, Z62SensorSim),
        VMSTATE_BOOL(firmware_av_valid, Z62SensorSim),
        VMSTATE_UINT64(firmware_link_updates, Z62SensorSim),
        VMSTATE_UINT64(firmware_link_errors, Z62SensorSim),
        VMSTATE_TIMER_PTR(stream_timer, Z62SensorSim),
        VMSTATE_END_OF_LIST()
    },
};

static void sensor_sim_class_init(ObjectClass *oc, const void *data)
{
    static const Property props[] = {
        DEFINE_PROP_LINK("root", Z62SensorSim, root,
                         TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_STRING("capture-prefix", Z62SensorSim, capture_prefix),
        DEFINE_PROP_BOOL("link-firmware-settings", Z62SensorSim,
                         link_firmware_settings, false),
        DEFINE_PROP_UINT32("tv-apex24-address", Z62SensorSim, tv_address, 0),
        DEFINE_PROP_UINT32("sv-apex24-address", Z62SensorSim, sv_address, 0),
        DEFINE_PROP_UINT32("av-apex24-address", Z62SensorSim, av_address, 0),
        DEFINE_PROP_BOOL("capture-packed12", Z62SensorSim,
                         capture_packed12, false),
    };
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = sensor_sim_realize;
    dc->unrealize = sensor_sim_unrealize;
    dc->vmsd = &sensor_sim_vmstate;
    dc->user_creatable = false;
    device_class_set_props(dc, props);
    RESETTABLE_CLASS(oc)->phases.hold = sensor_sim_reset;
    RESETTABLE_CLASS(oc)->phases.exit = sensor_sim_reset_exit;
}

static const TypeInfo sensor_sim_info = {
    .name = TYPE_Z62_SENSOR_SIM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Z62SensorSim),
    .instance_init = sensor_sim_init,
    .instance_finalize = sensor_sim_finalize,
    .class_init = sensor_sim_class_init,
};

static void sensor_sim_register(void)
{
    type_register_static(&sensor_sim_info);
}
type_init(sensor_sim_register)
