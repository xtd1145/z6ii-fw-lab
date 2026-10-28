/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Z6 II C1.70 sensor-path configuration at 20064000/20065000.
 * Addresses and acknowledgements: original helpers 10296900..10297498.
 * External inputs supply decoded status edges and result snapshots. The
 * default-off laboratory source associates one observed initialization
 * submit with its completion. Neither source generates pixels or DMA. */
#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#define TYPE_Z62_SENSOR_CONFIG "nikon-z62-sensor-config"
OBJECT_DECLARE_SIMPLE_TYPE(Z62SensorConfig, Z62_SENSOR_CONFIG)
#define CONFIG_SIZE 0x1600
#define WORD(s, off) ((s)->words[(off) / 4])
#define STATUS_BANKS 11
#define RESULT_WORDS 5

/* Mask association follows the paired original setters/getters. These are
 * decoded laboratory event lines, not a pinout recovered from silicon. */
static const struct {
    uint16_t status, mask;
    uint8_t irq;
} sensor_status[STATUS_BANKS] = {
    { 0x0014, 0x0010, 0 },
    { 0x1108, 0x110c, 1 },
    { 0x1024, 0x1028, 2 },
    { 0x1110, 0x1114, 2 },
    { 0x1118, 0x1120, 2 },
    { 0x111c, 0x1124, 2 },
    { 0x1508, 0x150c, 3 },
    { 0x1424, 0x1428, 4 },
    { 0x1510, 0x1514, 4 },
    { 0x1518, 0x1520, 4 },
    { 0x151c, 0x1524, 4 },
};
static const uint16_t sensor_results[RESULT_WORDS] = {
    0x0028, 0x1144, 0x1148, 0x1544, 0x1548,
};

struct Z62SensorConfig {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t words[CONFIG_SIZE / 4];
    uint32_t event_levels[STATUS_BANKS];
    qemu_irq irq[5];
    bool laboratory_init_completion;
    uint64_t init_delay_ns;
    bool submit_level, init_pending;
    uint64_t init_submissions, init_completions;
    QEMUTimer *init_timer;
    bool laboratory_mode_completion, mode_armed, mode_pending;
    uint64_t mode_completions;
    QEMUTimer *mode_timer;
    qemu_irq mode_irq;
    bool mode2_armed, mode2_wait_submit, mode2_pending;
    QEMUTimer *mode2_timer;
    qemu_irq mode2_irq;
    uint64_t mode2_completions;
    bool laboratory_frame_completion, frame_end_level;
    uint64_t frame_completions;
};

static void sensor_update_irqs(Z62SensorConfig *s)
{
    bool pending[5] = { false };
    for (unsigned i = 0; i < STATUS_BANKS; i++) {
        pending[sensor_status[i].irq] |=
            (WORD(s, sensor_status[i].status) &
             WORD(s, sensor_status[i].mask)) != 0;
    }
    for (unsigned i = 0; i < 5; i++) {
        qemu_set_irq(s->irq[i], pending[i]);
    }
}

static void sensor_event(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    unsigned bank = line / 32;
    uint32_t bit = 1u << (line % 32);
    if (level && !(s->event_levels[bank] & bit)) {
        WORD(s, sensor_status[bank].status) |= bit;
    }
    s->event_levels[bank] = level ? s->event_levels[bank] | bit :
                                   s->event_levels[bank] & ~bit;
    sensor_update_irqs(s);
}

static void sensor_result(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    /* A decoded result port carries one complete 32-bit word. Publishing a
     * snapshot has no interrupt side effect; the producer signals an event
     * separately, after supplying all associated result words. */
    WORD(s, sensor_results[line]) = (uint32_t)level;
}

static bool sensor_init_configured(Z62SensorConfig *s)
{
    return (WORD(s, 0x1400) & 0x10) && !(WORD(s, 0x1504) & 0xc000);
}

static bool sensor_mode2_configured(Z62SensorConfig *s)
{
    return (WORD(s, 0x1400) & 0x11) == 0x11 &&
           (WORD(s, 0x1504) & 0xc000) == 0x8000;
}

static void sensor_mode2_complete(void *opaque)
{
    Z62SensorConfig *s = opaque;
    s->mode2_pending = false;
    if (s->mode2_armed && sensor_mode2_configured(s)) {
        qemu_set_irq(s->mode2_irq, 1);
        qemu_set_irq(s->mode2_irq, 0);
        s->mode2_completions++;
    }
}

static void sensor_mode2_enable(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    s->mode2_armed = level != 0;
    if (!s->mode2_armed) {
        timer_del(s->mode2_timer);
        s->mode2_pending = s->mode2_wait_submit = false;
    }
}

static void sensor_mode2_submit(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    /* 102DD964 writes mode2, then submits command101 at 200632E0/E4.
     * The earlier mode0 flow submits before its mode2 write, so that
     * write alone must not produce the distinct sync bit18 response. */
    if (level && s->laboratory_mode_completion && s->mode2_armed &&
        s->mode2_wait_submit && sensor_mode2_configured(s)) {
        s->mode2_wait_submit = false;
        s->mode2_pending = true;
        timer_mod(s->mode2_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000);
    }
}

static void sensor_frame_end(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    bool edge = level && !s->frame_end_level;
    s->frame_end_level = level != 0;
    /* Optional decoded sensor-path completion at a supplied frame edge.
     * The original rearms IRQ92 bit1 on subsequent mode changes as well
     * as initialization. This supplies only that handshake, no pixels. */
    if (edge && s->laboratory_frame_completion &&
        sensor_mode2_configured(s) && (WORD(s, 0x150c) & 2)) {
        WORD(s, 0x1508) |= 2;
        s->frame_completions++;
        sensor_update_irqs(s);
    }
}

static void sensor_mode_complete(void *opaque)
{
    Z62SensorConfig *s = opaque;
    s->mode_pending = false;
    if (s->mode_armed && sensor_init_configured(s)) {
        /* Original 102DD9C8 waits for sync group2 bit10 after its explicit
         * bank1 mode0 write. No frame pixels or guest state are produced. */
        qemu_set_irq(s->mode_irq, 1);
        qemu_set_irq(s->mode_irq, 0);
        s->mode_completions++;
    }
}

static void sensor_mode_enable(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    s->mode_armed = level != 0;
    if (!s->mode_armed) {
        timer_del(s->mode_timer);
        s->mode_pending = false;
    }
}

static void sensor_init_complete(void *opaque)
{
    Z62SensorConfig *s = opaque;
    s->init_pending = false;
    if (sensor_init_configured(s)) {
        WORD(s, 0x1508) |= 2;
        s->init_completions++;
        sensor_update_irqs(s);
    }
}

static void sensor_submit(void *opaque, int line, int level)
{
    Z62SensorConfig *s = opaque;
    bool edge = level && !s->submit_level;
    s->submit_level = level != 0;
    if (!edge) {
        return;
    }
    s->init_submissions++;
    /* Opt-in functional experiment: the original submits at 20068104=1
     * and then waits for IRQ92 bit1. Its exact silicon cause is unresolved.
     * Restrict this association to one initialization completion per reset,
     * after the observed bank1 setup, with the consumer already armed.
     * No guest state, result snapshot or image buffer is written here. */
    if (s->laboratory_init_completion && !s->init_completions &&
        !s->init_pending && sensor_init_configured(s) &&
        (WORD(s, 0x150c) & 2)) {
        s->init_pending = true;
        timer_mod(s->init_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->init_delay_ns);
    }
}

static bool sensor_config_status(hwaddr off)
{
    if (off == 0x14) {
        return true;
    }
    if (off >= 0x1400) {
        off -= 0x400;
    }
    return off == 0x1024 || off == 0x1108 || off == 0x1110 ||
           off == 0x1118 || off == 0x111c;
}

static bool sensor_config_result(hwaddr off)
{
    return off == 0x28 || off == 0x1144 || off == 0x1148 ||
           off == 0x1544 || off == 0x1548;
}

static bool sensor_config_accepts(void *opaque, hwaddr off, unsigned size,
                                  bool is_write, MemTxAttrs attrs)
{
    if (size != 4 || (off & 3)) {
        return false;
    }
    if (off < 0x1000) {
        switch (off) {
        case 0: case 4: case 8: case 0x10: case 0x14: case 0x18:
        case 0x1c: case 0x24: case 0x28: case 0x30: case 0x100:
        case 0x104: case 0x110: case 0x114: case 0x118: case 0x11c:
            return true;
        default:
            return false;
        }
    }
    /* Two symmetric banks, selected by the original helpers' bank argument. */
    if (off >= 0x1400) {
        off -= 0x400;
    }
    switch (off) {
    case 0x1000: case 0x1004: case 0x1008: case 0x100c:
    case 0x1014: case 0x1018: case 0x101c: case 0x1020:
    case 0x1024: case 0x1028:
    case 0x1104: case 0x1108: case 0x110c: case 0x1110:
    case 0x1114: case 0x1118: case 0x111c: case 0x1120:
    case 0x1124: case 0x1128: case 0x112c: case 0x1130:
    case 0x1134: case 0x1138: case 0x113c: case 0x1140:
    case 0x1144: case 0x1148: case 0x1150:
        return true;
    default:
        return false;
    }
}

static uint64_t sensor_config_read(void *opaque, hwaddr off, unsigned size)
{
    Z62SensorConfig *s = opaque;
    return WORD(s, off);
}

static void sensor_config_write(void *opaque, hwaddr off, uint64_t value,
                                 unsigned size)
{
    Z62SensorConfig *s = opaque;
    if (sensor_config_status(off)) {
        /* IRQ89..93 handlers read a status and write the same word to ack.
         * A software acknowledgement must never create a pending event. */
        WORD(s, off) &= ~value;
    } else if (!sensor_config_result(off)) {
        WORD(s, off) = value;
    }
    if (off == 0x1504 || off == 0x1400) {
        if (off == 0x1504 || !sensor_mode2_configured(s)) {
            timer_del(s->mode2_timer);
            s->mode2_pending = s->mode2_wait_submit = false;
        }
        if (off == 0x1504 && s->laboratory_mode_completion &&
            s->mode2_armed && sensor_mode2_configured(s)) {
            s->mode2_wait_submit = true;
        }
        if (off == 0x1504 || !sensor_init_configured(s)) {
            timer_del(s->mode_timer);
            s->mode_pending = false;
        }
        /* A same-value mode0 write is still a request. Other modes cancel
         * this response; mode2 uses a subsequent command, not bit10. */
        if (off == 0x1504 && s->laboratory_mode_completion &&
            s->mode_armed && sensor_init_configured(s)) {
            s->mode_pending = true;
            timer_mod(s->mode_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000);
        }
    }
    sensor_update_irqs(s);
}

static const MemoryRegionOps sensor_config_ops = {
    .read = sensor_config_read,
    .write = sensor_config_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4,
               .accepts = sensor_config_accepts },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void sensor_config_reset(Object *obj, ResetType type)
{
    Z62SensorConfig *s = Z62_SENSOR_CONFIG(obj);
    memset(s->words, 0, sizeof(s->words));
    memset(s->event_levels, 0, sizeof(s->event_levels));
    timer_del(s->init_timer);
    timer_del(s->mode_timer);
    timer_del(s->mode2_timer);
    s->mode2_armed = s->mode2_wait_submit = s->mode2_pending = false;
    s->mode2_completions = 0;
    s->frame_end_level = false;
    s->frame_completions = 0;
    qemu_set_irq(s->mode2_irq, 0);
    s->mode_pending = s->mode_armed = false;
    s->mode_completions = 0;
    qemu_set_irq(s->mode_irq, 0);
    s->submit_level = s->init_pending = false;
    s->init_submissions = s->init_completions = 0;
    sensor_update_irqs(s);
}

static int sensor_config_post_load(void *opaque, int version_id)
{
    sensor_update_irqs(opaque);
    return 0;
}

static const VMStateDescription sensor_config_vmstate = {
    .name = TYPE_Z62_SENSOR_CONFIG,
    .version_id = 6,
    .minimum_version_id = 6,
    .post_load = sensor_config_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(words, Z62SensorConfig, CONFIG_SIZE / 4),
        VMSTATE_UINT32_ARRAY(event_levels, Z62SensorConfig, STATUS_BANKS),
        VMSTATE_BOOL(submit_level, Z62SensorConfig),
        VMSTATE_BOOL(init_pending, Z62SensorConfig),
        VMSTATE_UINT64(init_submissions, Z62SensorConfig),
        VMSTATE_UINT64(init_completions, Z62SensorConfig),
        VMSTATE_TIMER_PTR(init_timer, Z62SensorConfig),
        VMSTATE_BOOL(mode_armed, Z62SensorConfig),
        VMSTATE_BOOL(mode_pending, Z62SensorConfig),
        VMSTATE_UINT64(mode_completions, Z62SensorConfig),
        VMSTATE_TIMER_PTR(mode_timer, Z62SensorConfig),
        VMSTATE_BOOL(mode2_armed, Z62SensorConfig),
        VMSTATE_BOOL(mode2_wait_submit, Z62SensorConfig),
        VMSTATE_BOOL(mode2_pending, Z62SensorConfig),
        VMSTATE_TIMER_PTR(mode2_timer, Z62SensorConfig),
        VMSTATE_UINT64(mode2_completions, Z62SensorConfig),
        VMSTATE_BOOL(frame_end_level, Z62SensorConfig),
        VMSTATE_UINT64(frame_completions, Z62SensorConfig),
        VMSTATE_END_OF_LIST()
    },
};

static void sensor_config_init(Object *obj)
{
    Z62SensorConfig *s = Z62_SENSOR_CONFIG(obj);
    memory_region_init_io(&s->iomem, obj, &sensor_config_ops, s,
                          TYPE_Z62_SENSOR_CONFIG, CONFIG_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    for (unsigned i = 0; i < 5; i++) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq[i]);
    }
    qdev_init_gpio_in_named(DEVICE(s), sensor_event, "sensor-event",
                           STATUS_BANKS * 32);
    qdev_init_gpio_in_named(DEVICE(s), sensor_result, "sensor-result",
                           RESULT_WORDS);
    qdev_init_gpio_in_named(DEVICE(s), sensor_submit, "submit", 1);
    qdev_init_gpio_in_named(DEVICE(s), sensor_mode_enable, "mode-enable", 1);
    qdev_init_gpio_out_named(DEVICE(s), &s->mode_irq, "mode-ready", 1);
    qdev_init_gpio_in_named(DEVICE(s), sensor_mode2_enable, "mode2-enable", 1);
    qdev_init_gpio_in_named(DEVICE(s), sensor_mode2_submit, "mode2-submit", 1);
    qdev_init_gpio_out_named(DEVICE(s), &s->mode2_irq, "mode2-ready", 1);
    qdev_init_gpio_in_named(DEVICE(s), sensor_frame_end, "frame-end", 1);
    object_property_add_uint64_ptr(obj, "frame-completions", &s->frame_completions,
                                  OBJ_PROP_FLAG_READ);
    s->init_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sensor_init_complete, s);
    s->mode_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sensor_mode_complete, s);
    s->mode2_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sensor_mode2_complete, s);
    object_property_add_uint64_ptr(obj, "mode2-completions", &s->mode2_completions,
                                  OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "mode-completions", &s->mode_completions,
                                  OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "init-submissions", &s->init_submissions,
                                  OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "init-completions", &s->init_completions,
                                  OBJ_PROP_FLAG_READ);
}

static void sensor_config_finalize(Object *obj)
{
    Z62SensorConfig *s = Z62_SENSOR_CONFIG(obj);
    timer_free(s->init_timer);
    timer_free(s->mode_timer);
    timer_free(s->mode2_timer);
}

static void sensor_config_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    static const Property properties[] = {
        DEFINE_PROP_BOOL("laboratory-init-completion", Z62SensorConfig,
                         laboratory_init_completion, false),
        DEFINE_PROP_BOOL("laboratory-mode-completion", Z62SensorConfig,
                         laboratory_mode_completion, false),
        DEFINE_PROP_BOOL("laboratory-frame-completion", Z62SensorConfig,
                         laboratory_frame_completion, false),
        DEFINE_PROP_UINT64("init-delay-ns", Z62SensorConfig, init_delay_ns,
                           33333333),
    };
    device_class_set_props(dc, properties);
    dc->vmsd = &sensor_config_vmstate;
    dc->user_creatable = false;
    RESETTABLE_CLASS(oc)->phases.hold = sensor_config_reset;
}

static const TypeInfo sensor_config_info = {
    .name = TYPE_Z62_SENSOR_CONFIG,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Z62SensorConfig),
    .instance_init = sensor_config_init,
    .instance_finalize = sensor_config_finalize,
    .class_init = sensor_config_class_init,
};

static void sensor_config_register_types(void)
{
    type_register_static(&sensor_config_info);
}
type_init(sensor_config_register_types)
