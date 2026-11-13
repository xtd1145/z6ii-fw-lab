/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include "nikon-gpu.h"
#include "nikon-z62-sensor-sim.h"

#define TYPE_Z72_GRAPHICS "nikon-z72-graphics"
OBJECT_DECLARE_SIMPLE_TYPE(Z72Graphics, Z72_GRAPHICS)

uint32_t nikon_gpu_lab_lv_plane;

struct Z72Graphics {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    MemoryRegion *root;
    AddressSpace memory;
    NikonGPU *gpu;
    Z62SensorSim *sensor;
    uint32_t registers[0x2000 / 4];
    bool pending[2];
    bool drawing;
    bool draw_complete;
    unsigned completed_draws;
    uint32_t successful_uploads;
    uint32_t render_requests;
    uint32_t failed_requests;
    uint32_t display_width, display_height;
    bool test_frame;
    uint32_t test_source;
    bool live_replay;
    uint32_t replay_interval_ns;
    uint64_t replay_runs;
    QEMUTimer *timer;
    QEMUTimer *replay_timer;
    qemu_irq irqs[3];
};

static void graphics_irqs(Z72Graphics *s)
{
    qemu_set_irq(s->irqs[0], s->registers[0x1020 / 4] != 0);
    qemu_set_irq(s->irqs[1], s->draw_complete);
    qemu_set_irq(s->irqs[2], s->registers[0xc18 / 4] != 0);
}

static void graphics_replay(void *opaque)
{
    Z72Graphics *s = opaque;
    if (!s->gpu || !s->gpu->live_replay) {
        return;
    }
    if (nikon_gpu_replay_latest(s->gpu)) {
        nikon_gpu_lab_lv_plane = s->gpu->replay_src_addr;
        if (s->replay_runs++ == 0) {
            fprintf(stderr, "Z72_GPU_REPLAY first batch applied\n");
        }
    }
    timer_mod(s->replay_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                               MAX(s->replay_interval_ns, 1000000));
}

static void graphics_complete(void *opaque)
{
    Z72Graphics *s = opaque;
    bool completed = false;
    if (s->gpu) {
        s->gpu->test_frame = s->test_frame;
        s->gpu->test_filter = s->test_source;
    }
    for (unsigned channel = 0; channel < 2; channel++) {
        if (!s->pending[channel]) {
            continue;
        }
        unsigned offset = 0x10d0 + channel * 0x10;
        uint32_t source = s->registers[(offset + 4) / 4];
        uint64_t bytes = (uint64_t)s->registers[(offset + 12) / 4] * 8;
        s->pending[channel] = false;
        s->registers[offset / 4] = 0;
        if (!bytes || bytes > 0x80000 ||
            !nikon_gpu_range(s->gpu, source, bytes, false)) {
            s->registers[0xc18 / 4] = 1;
            s->failed_requests++;
            continue;
        }
        g_autofree uint8_t *commands = g_malloc(bytes);
        nikon_gpu_read(s->gpu, source, commands, bytes);
        unsigned errors = s->gpu->unsupported;
        nikon_gpu_execute(s->gpu, channel, commands, bytes);
        if (s->gpu->unsupported != errors) {
            s->registers[0xc18 / 4] = 1;
            s->failed_requests++;
        } else {
            completed = true;
            s->successful_uploads++;
        }
    }
    if (completed) {
        s->registers[0x1020 / 4] = 1;
    }
    if (s->drawing) {
        s->drawing = false;
        if (s->gpu->rendered > s->completed_draws) {
            s->completed_draws = s->gpu->rendered;
            s->draw_complete = true;
            s->registers[0x1c / 4] = 1;
        } else {
            s->registers[0xc18 / 4] = 1;
            s->failed_requests++;
        }
    }
    graphics_irqs(s);
}

static uint64_t graphics_read(void *opaque, hwaddr offset, unsigned size)
{
    Z72Graphics *s = opaque;
    if (offset == 0x58) {
        return (s->pending[0] || s->pending[1]) ? 1u << 26 : 0;
    }
    return s->registers[offset / 4];
}

static void graphics_write(void *opaque, hwaddr offset, uint64_t value,
                           unsigned size)
{
    Z72Graphics *s = opaque;
    if (offset == 0x58) {
        return;
    }
    s->registers[offset / 4] = value;
    if (offset == 0x1c) {
        s->draw_complete = false;
        s->drawing = value != 0;
        if (s->drawing) {
            s->render_requests++;
            timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
        }
    }
    if (offset == 0x10d0 || offset == 0x10e0) {
        unsigned channel = (offset - 0x10d0) / 0x10;
        s->pending[channel] = value & 1;
        if (s->pending[channel]) {
            timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
        }
    }
    graphics_irqs(s);
}

static const MemoryRegionOps graphics_ops = {
    .read = graphics_read,
    .write = graphics_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4,
               .unaligned = false },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void graphics_reset(Object *obj, ResetType type)
{
    Z72Graphics *s = Z72_GRAPHICS(obj);
    timer_del(s->timer);
    timer_del(s->replay_timer);
    s->replay_runs = 0;
    nikon_gpu_lab_lv_plane = 0;
    memset(s->registers, 0, sizeof(s->registers));
    memset(s->pending, 0, sizeof(s->pending));
    s->drawing = s->draw_complete = false;
    s->completed_draws = 0;
    s->successful_uploads = 0;
    s->render_requests = s->failed_requests = 0;
    if (s->gpu) {
        g_hash_table_remove_all(s->gpu->paths);
        if (s->gpu->active) {
            nikon_gpu_path_free(s->gpu->active);
            s->gpu->active = NULL;
        }
        memset(s->gpu->regs, 0, sizeof(s->gpu->regs));
        s->gpu->present_address = 0;
        s->gpu->lv_address = 0;
        g_hash_table_remove_all(s->gpu->batches);
        s->gpu->in_replay = false;
        s->gpu->last_draw_ns = 0;
        s->gpu->replay_src_addr = s->gpu->replay_src_bytes = 0;
        s->gpu->rendered = s->gpu->unsupported = 0;
        s->gpu->stroke_samples = s->gpu->icon_samples = 0;
        s->gpu->test_seq = 0;
        s->gpu->test_logged = 0;
        s->gpu->test_dumps = 0;
        DisplaySurface *surface = qemu_console_surface(s->gpu->console);
        memset(surface_data(surface), 0, surface_stride(surface) * surface_height(surface));
        qemu_console_update(s->gpu->console, 0, 0, surface_width(surface), surface_height(surface));
    }
    graphics_irqs(s);
}

static void graphics_reset_exit(Object *obj, ResetType type)
{
    Z72Graphics *s = Z72_GRAPHICS(obj);
    if (s->gpu && s->gpu->live_replay) {
        timer_mod(s->replay_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   MAX(s->replay_interval_ns, 1000000));
    }
}

static void graphics_realize(DeviceState *dev, Error **errp)
{
    Z72Graphics *s = Z72_GRAPHICS(dev);
    if (!s->root) {
        error_setg(errp, "Graphics requires an engine memory view");
        return;
    }
    if (!s->display_width || !s->display_height ||
        s->display_width > 4096 || s->display_height > 4096) {
        error_setg(errp, "Graphics display geometry must be within 1..4096");
        return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z72_GRAPHICS);
    s->gpu = nikon_gpu_new();
    s->gpu->memory = &s->memory;
    s->gpu->sensor = s->sensor;
    if (getenv("Z72_GPU_TEST_FRAME")) {
        s->test_frame = true;
    }
    if (getenv("Z72_GPU_TEST_FILTER")) {
        s->test_source = strtoul(getenv("Z72_GPU_TEST_FILTER"), NULL, 16);
    }
    s->gpu->test_frame = s->test_frame;
    s->gpu->test_filter = s->test_source;
    s->gpu->live_replay = s->live_replay;
    s->gpu->lv_mirror = s->live_replay;
    if (s->live_replay) {
        fprintf(stderr, "Z72_GPU_REPLAY enabled interval_ns=%u\n",
                s->replay_interval_ns);
        timer_mod(s->replay_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   MAX(s->replay_interval_ns, 1000000));
    }
    s->gpu->display_width = s->display_width;
    s->gpu->display_height = s->display_height;
    qemu_console_resize(s->gpu->console, s->display_width, s->display_height);
}

static void graphics_init(Object *obj)
{
    Z72Graphics *s = Z72_GRAPHICS(obj);
    memory_region_init_io(&s->iomem, obj, &graphics_ops, s,
                         TYPE_Z72_GRAPHICS, 0x2000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    for (unsigned line = 0; line < 3; line++) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irqs[line]);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, graphics_complete, s);
    s->replay_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, graphics_replay, s);
    s->replay_interval_ns = 66666666;
    object_property_add_uint64_ptr(obj, "replay-runs", &s->replay_runs,
                                   OBJ_PROP_FLAG_READ);
}

static void graphics_unrealize(DeviceState *dev)
{
    Z72Graphics *s = Z72_GRAPHICS(dev);
    timer_del(s->timer);
    timer_del(s->replay_timer);
    graphics_reset(OBJECT(s), RESET_TYPE_COLD);
    if (s->gpu) {
        g_hash_table_destroy(s->gpu->batches);
        qemu_graphic_console_close(s->gpu->console);
        g_hash_table_destroy(s->gpu->paths);
        g_free(s->gpu);
        s->gpu = NULL;
        address_space_destroy(&s->memory);
    }
}

static void graphics_finalize(Object *obj)
{
    Z72Graphics *s = Z72_GRAPHICS(obj);
    timer_free(s->timer);
    timer_free(s->replay_timer);
}

static void graphics_observation(Object *obj, Visitor *visitor, const char *name,
                                 void *opaque, Error **errp)
{
    Z72Graphics *s = Z72_GRAPHICS(obj);
    uint32_t value = 0;
    unsigned field = GPOINTER_TO_UINT(opaque);
    if (field == 0) {
        value = s->successful_uploads;
    } else if (field == 4) {
        value = s->render_requests;
    } else if (field == 5) {
        value = s->failed_requests;
    } else if (s->gpu) {
        if (field == 1) {
            value = s->gpu->rendered;
        } else if (field == 2) {
            value = s->gpu->unsupported;
        } else if (field == 3) {
            value = s->gpu->present_address;
        } else {
            static const unsigned registers[] = { 0x1a4, 0x1a3, 0x1a2, 0x1b5 };
            value = s->gpu->regs[1][registers[field - 6]];
        }
    }
    visit_type_uint32(visitor, name, &value, errp);
}

/* Dynamic path batches are host-side caches of guest draw commands. They are
 * deliberately not transferred: the guest framebuffer lives in migrated RAM,
 * so post_load clears the caches and repaints from that buffer. Only a pending
 * command completion is re-armed. */
static int graphics_post_load(void *opaque, int version_id)
{
    Z72Graphics *s = opaque;
    NikonGPU *g = s->gpu;

    if (g) {
        g_hash_table_remove_all(g->paths);
        if (g->active) {
            nikon_gpu_path_free(g->active);
            g->active = NULL;
        }
        g_hash_table_remove_all(g->batches);
        g->in_replay = false;
        if (g->present_address) {
            nikon_gpu_present(g);
        }
    }
    if ((s->pending[0] || s->pending[1] || s->drawing) &&
        !timer_pending(s->timer)) {
        timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
    }
    if (g && g->live_replay && !timer_pending(s->replay_timer)) {
        timer_mod(s->replay_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   MAX(s->replay_interval_ns, 1000000));
    }
    graphics_irqs(s);
    return 0;
}

static const VMStateDescription gpu_vmstate = {
    .name = "nikon-gpu",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_2DARRAY(regs, NikonGPU, 2, 512),
        VMSTATE_UINT32(present_address, NikonGPU),
        VMSTATE_UINT32(lv_address, NikonGPU),
        VMSTATE_UINT32(rendered, NikonGPU),
        VMSTATE_UINT32(unsupported, NikonGPU),
        VMSTATE_UINT32(stroke_samples, NikonGPU),
        VMSTATE_UINT32(icon_samples, NikonGPU),
        VMSTATE_BOOL(lcd_scanout, NikonGPU),
        VMSTATE_BOOL(test_frame, NikonGPU),
        VMSTATE_UINT32(test_filter, NikonGPU),
        VMSTATE_UINT64(test_seq, NikonGPU),
        VMSTATE_UINT32(test_logged, NikonGPU),
        VMSTATE_UINT32(test_dumps, NikonGPU),
        VMSTATE_BOOL(live_replay, NikonGPU),
        VMSTATE_UINT64(last_draw_ns, NikonGPU),
        VMSTATE_BOOL(lv_mirror, NikonGPU),
        VMSTATE_UINT32(replay_src_addr, NikonGPU),
        VMSTATE_UINT32(replay_src_bytes, NikonGPU),
        VMSTATE_END_OF_LIST()
    },
};

static const VMStateDescription graphics_vmstate = {
    .name = TYPE_Z72_GRAPHICS,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = graphics_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(registers, Z72Graphics, 0x2000 / 4),
        VMSTATE_BOOL_ARRAY(pending, Z72Graphics, 2),
        VMSTATE_BOOL(drawing, Z72Graphics),
        VMSTATE_BOOL(draw_complete, Z72Graphics),
        VMSTATE_UINT32(completed_draws, Z72Graphics),
        VMSTATE_UINT32(successful_uploads, Z72Graphics),
        VMSTATE_UINT32(render_requests, Z72Graphics),
        VMSTATE_UINT32(failed_requests, Z72Graphics),
        VMSTATE_UINT64(replay_runs, Z72Graphics),
        VMSTATE_TIMER_PTR(timer, Z72Graphics),
        VMSTATE_TIMER_PTR(replay_timer, Z72Graphics),
        VMSTATE_STRUCT_POINTER(gpu, Z72Graphics, gpu_vmstate, NikonGPU),
        VMSTATE_END_OF_LIST()
    },
};

static void graphics_class_init(ObjectClass *klass, const void *data)
{
    static const Property properties[] = {
        DEFINE_PROP_LINK("root", Z72Graphics, root,
                         TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_LINK("sensor", Z72Graphics, sensor,
                         TYPE_Z62_SENSOR_SIM, Z62SensorSim *),
        DEFINE_PROP_UINT32("display-width", Z72Graphics, display_width, 720),
        DEFINE_PROP_UINT32("display-height", Z72Graphics, display_height, 480),
        DEFINE_PROP_BOOL("test-frame", Z72Graphics, test_frame, false),
        DEFINE_PROP_UINT32("test-frame-source", Z72Graphics, test_source, 0),
        DEFINE_PROP_BOOL("live-replay", Z72Graphics, live_replay, false),
        DEFINE_PROP_UINT32("replay-interval-ns", Z72Graphics, replay_interval_ns,
                           66666666),
    };
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = graphics_realize;
    dc->unrealize = graphics_unrealize;
    dc->vmsd = &graphics_vmstate;
    device_class_set_props(dc, properties);
    static const char *const observations[] = {
        "successful-uploads", "rendered-commands", "unsupported-commands", "surface-address",
        "render-requests", "failed-requests",
        "target-address", "target-dimensions", "target-format", "target-second-plane",
    };
    for (unsigned field = 0; field < G_N_ELEMENTS(observations); field++) {
        object_class_property_add(klass, observations[field], "uint32",
                                  graphics_observation, NULL, NULL, GUINT_TO_POINTER(field));
    }
    RESETTABLE_CLASS(klass)->phases.hold = graphics_reset;
    RESETTABLE_CLASS(klass)->phases.exit = graphics_reset_exit;
}

static void graphics_register(void)
{
    static const TypeInfo info = {
        .name = TYPE_Z72_GRAPHICS,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(Z72Graphics),
        .instance_init = graphics_init,
        .instance_finalize = graphics_finalize,
        .class_init = graphics_class_init,
    };
    type_register_static(&info);
}
type_init(graphics_register)
