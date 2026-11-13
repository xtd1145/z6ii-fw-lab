/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Z62 C1.70 CVP mailbox/control laboratory.
 * Register/FIFO/IRQ contracts follow 1024295C, 10242A08, 10242ADE,
 * 10242C4C and 10242CBE. Boot message timing and [1,0,0] are explicit
 * opt-in functional assumptions, not recovered IPU execution.
 *
 * No detector, TL4 execution, image reads or pixel results exist, and none
 * are claimed. What `laboratory-compute` adds is protocol completeness: any
 * submitted three-word command that is not a control ACK gets a completion
 * with the same opcode-plus-0xb000 relation the firmware itself uses for the
 * setup path (0x900e -> 0xb00e, both observed in traces). The arguments are
 * echoed verbatim and no guest memory is written, so the mailbox can answer a
 * request without inventing detection output. A test can therefore exercise
 * the firmware's command/completion plumbing while the result *values* stay
 * explicitly unverified. */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/bswap.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/resettable.h"
#include "migration/vmstate.h"
#include "system/address-spaces.h"

#define TYPE_Z62_CVP_CONTROL "nikon-z62-cvp-control"
OBJECT_DECLARE_SIMPLE_TYPE(Z62CvpControl, Z62_CVP_CONTROL)
#define CVP_RX_WORDS 96

struct Z62CvpControl {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq;
    QEMUTimer *timer;
    QEMUTimer *setup_timer;
    QEMUTimer *compute_timer;
    MemoryRegion *root;
    AddressSpace memory;
    uint32_t regs[0x98 / 4];
    uint32_t rx[CVP_RX_WORDS], rx_head, rx_count, rx_cursor;
    uint32_t tx[3], tx_count;
    bool laboratory_handshake, trace, boot_pending, reset_asserted;
    bool laboratory_manager_setup, setup_pending;
    uint32_t setup_descriptor, setup_opaque;
    uint64_t delay_ns, boot_completions, commands, unsupported;
    uint64_t setup_completions;
    /* Generic command completion. Every submitted three-word command that is
     * neither a control ACK nor the manager setup gets an echo completion with
     * bit 0xb000 folded into the opcode - the exact relation the firmware
     * itself uses for the setup path (0x900e -> 0xb00e, both observed). This
     * makes the mailbox able to answer *any* request instead of silently
     * dropping it, without inventing a payload: the arguments are echoed
     * unchanged, no guest memory is written and no result is claimed. */
    bool laboratory_compute;
    bool compute_pending;
    uint32_t compute_op, compute_a1, compute_a2;
    uint64_t compute_delay_ns;
    uint64_t compute_requests;
    uint64_t compute_completions;
    uint64_t compute_dropped;
};

static void cvp_irq(Z62CvpControl *s)
{
    qemu_set_irq(s->irq, !!(s->regs[0x14 / 4] & s->regs[0x1c / 4]));
}

static void cvp_boot_complete(void *opaque)
{
    Z62CvpControl *s = opaque;
    s->boot_pending = false;
    if (!s->laboratory_handshake || s->reset_asserted || s->rx_count) {
        return;
    }
    /* RX dispatch opcode 1 sets the original initialization event. The
     * guest must consume this packet, run its ISR/worker and acknowledge it. */
    s->rx_head = 0;
    s->rx[0] = 1;
    s->rx[1] = s->rx[2] = 0;
    s->rx_count = 3;
    s->regs[0x14 / 4] |= 1;
    s->boot_completions++;
    if (s->trace) {
        fprintf(stderr, "z62.cvp boot message [00000001 00000000 00000000], IRQ101\n");
    }
    cvp_irq(s);
}

static bool cvp_setup_descriptor(Z62CvpControl *s, uint32_t address)
{
    uint8_t bytes[28];
    hwaddr translated, available = sizeof(bytes);
    MemoryRegion *mr = address_space_translate(&s->memory, address,
        &translated, &available, false, MEMTXATTRS_UNSPECIFIED);
    if (!memory_region_is_ram(mr) || available < sizeof(bytes) ||
        address_space_read(&s->memory, address, MEMTXATTRS_UNSPECIFIED,
                           bytes, sizeof(bytes)) != MEMTX_OK) {
        return false;
    }
    for (unsigned i = 0; i < 7; i++) {
        uint32_t expected = i == 0 ? 0xe : i == 2 ? 1 : 0;
        if (ldl_le_p(bytes + i * 4) != expected) {
            return false;
        }
    }
    return true;
}

static void cvp_setup_complete(void *opaque)
{
    Z62CvpControl *s = opaque;
    if (!s->laboratory_manager_setup || s->reset_asserted || !s->setup_pending) {
        return;
    }
    if (s->rx_count > CVP_RX_WORDS - 6) {
        timer_mod(s->setup_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->delay_ns);
        return;
    }
    /* Explicit synthetic manager-setup notification, not execution of the
     * IPU opcode E. Its exact internal side effects are unknown. First
     * release the pending request, then notify the original manager. */
    uint32_t words[] = {0xb00e, s->setup_descriptor, s->setup_opaque,
                       0x000f, s->setup_descriptor, s->setup_opaque};
    for (unsigned i = 0; i < G_N_ELEMENTS(words); i++) {
        s->rx[(s->rx_head + s->rx_count++) % CVP_RX_WORDS] = words[i];
    }
    s->setup_pending = false;
    s->setup_completions++;
    s->regs[0x14 / 4] |= 1;
    if (s->trace) {
        fprintf(stderr, "z62.cvp LAB manager setup B00E/F notifications; no IPU execution\n");
    }
    cvp_irq(s);
}

static void cvp_compute_complete(void *opaque)
{
    Z62CvpControl *s = opaque;
    s->compute_pending = false;
    if (!s->laboratory_compute || s->reset_asserted || !s->compute_op) {
        return;
    }
    if (s->rx_count > CVP_RX_WORDS - 3) {
        /* FIFO full: drop rather than corrupt the queue, and say so. */
        s->compute_dropped++;
        if (s->trace) {
            fprintf(stderr, "z62.cvp compute completion dropped (RX full)\n");
        }
        return;
    }
    uint32_t words[] = { s->compute_op | 0xb000u, s->compute_a1,
                         s->compute_a2 };
    for (unsigned i = 0; i < G_N_ELEMENTS(words); i++) {
        s->rx[(s->rx_head + s->rx_count++) % CVP_RX_WORDS] = words[i];
    }
    s->compute_completions++;
    s->regs[0x14 / 4] |= 1;
    if (s->trace) {
        fprintf(stderr, "z62.cvp compute completion [%08x %08x %08x]"
                " n=%" PRIu64 " (echo only, no payload)\n",
                words[0], words[1], words[2], s->compute_completions);
    }
    cvp_irq(s);
}

static MemTxResult cvp_read(void *opaque, hwaddr offset, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    Z62CvpControl *s = opaque;
    if (size != 4 || (offset & 3) || offset >= sizeof(s->regs)) {
        return MEMTX_ERROR;
    }
    if (getenv("Z62_CVP_TRACE")) {
        static unsigned traced;
        if (traced++ < 120) {
            fprintf(stderr, "Z62_CVP rd off=%02" HWADDR_PRIx "\n", offset);
        }
    }
    switch (offset) {
    case 0:
        if (s->rx_cursor >= MIN(3u, s->rx_count)) {
            return MEMTX_ERROR;
        }
        *value = s->rx[(s->rx_head + s->rx_cursor) % CVP_RX_WORDS];
        s->rx_cursor++;
        break;
    case 0xc:
        *value = s->rx_count; /* TX occupancy is zero: commands drain at submit. */
        break;
    case 0x80:
        *value = 0x100; /* Original driver requires this interface value. */
        break;
    case 0x18:
        *value = 0; /* W1C acknowledgement, never passive writable pending. */
        break;
    case 4:
        return MEMTX_ERROR;
    default:
        *value = s->regs[offset / 4];
        break;
    }
    return MEMTX_OK;
}

static MemTxResult cvp_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    Z62CvpControl *s = opaque;
    if (size != 4 || (offset & 3) || offset >= sizeof(s->regs)) {
        return MEMTX_ERROR;
    }
    if (getenv("Z62_CVP_TRACE")) {
        static unsigned traced;
        if (traced++ < 120) {
            fprintf(stderr, "Z62_CVP wr off=%02" HWADDR_PRIx " = %08x\n",
                    offset, (unsigned)value);
        }
    }
    switch (offset) {
    case 0: case 0xc: case 0x14: case 0x80:
        return MEMTX_ERROR;
    case 4:
        if (s->tx_count == G_N_ELEMENTS(s->tx)) {
            return MEMTX_ERROR;
        }
        s->tx[s->tx_count++] = value;
        return MEMTX_OK;
    case 8:
        if (value == 2) {
            if (s->tx_count != 3) {
                s->tx_count = 0;
                return MEMTX_ERROR;
            }
            s->commands++;
            bool setup = s->laboratory_handshake && s->laboratory_manager_setup &&
                         s->tx[0] == 0x900e && s->tx[2] == 0 &&
                         !s->setup_pending && !s->reset_asserted &&
                         cvp_setup_descriptor(s, s->tx[1]);
            bool ack = s->tx[0] == 0x2001 || s->tx[0] == 0x200f;
            if (setup) {
                s->setup_descriptor = s->tx[1];
                s->setup_opaque = s->tx[2];
                s->setup_pending = true;
                timer_mod(s->setup_timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->delay_ns);
            } else if (!ack) {
                if (s->laboratory_compute && !s->reset_asserted) {
                    if (s->compute_pending) {
                        s->compute_dropped++;
                    } else {
                        s->compute_op = s->tx[0];
                        s->compute_a1 = s->tx[1];
                        s->compute_a2 = s->tx[2];
                        s->compute_pending = true;
                        s->compute_requests++;
                        timer_mod(s->compute_timer,
                                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                  s->compute_delay_ns);
                    }
                } else {
                    s->unsupported++;
                }
            }
            if (s->trace) {
                const char *what = ack ? "control ACK" :
                                   setup ? "LAB manager setup only" :
                                   s->laboratory_compute ?
                                       "compute request (echo completion)" :
                                       "unsupported; no completion";
                fprintf(stderr, "z62.cvp TX [%08x %08x %08x] %s\n",
                    s->tx[0], s->tx[1], s->tx[2], what);
            }
            s->tx_count = 0;
        } else if (value == 4) {
            /* Both original RX consumption (three reads then 4) and startup
             * drain (4 without reads) must remove a packet exactly once.
             * Packet-granular commit is a laboratory FIFO contract. */
            unsigned consumed = MIN(3u, s->rx_count);
            s->rx_head = (s->rx_head + consumed) % CVP_RX_WORDS;
            s->rx_count -= consumed;
            s->rx_cursor = 0;
        } else {
            return MEMTX_ERROR;
        }
        /* Pending is cleared independently at 18, as the original ISR does. */
        return MEMTX_OK;
    case 0x18:
        s->regs[0x14 / 4] &= ~(uint32_t)value;
        break;
    case 0x84:
        s->regs[offset / 4] = value;
        if (value == 1) {
            s->reset_asserted = true;
            s->boot_pending = false;
            timer_del(s->timer);
            timer_del(s->setup_timer);
            timer_del(s->compute_timer);
            s->setup_pending = false;
            s->compute_pending = false;
            s->rx_head = s->rx_count = s->rx_cursor = s->tx_count = 0;
            s->regs[0x14 / 4] = 0;
        } else if (value == 0 && s->reset_asserted) {
            s->reset_asserted = false;
            if (s->laboratory_handshake && s->regs[0x88 / 4] &&
                s->regs[0x8c / 4] && s->regs[0x90 / 4] &&
                s->regs[0x94 / 4] &&
                !((s->regs[0x88 / 4] | s->regs[0x8c / 4] |
                   s->regs[0x90 / 4] | s->regs[0x94 / 4]) & 31)) {
                s->boot_pending = true;
                timer_mod(s->timer,
                    qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->delay_ns);
            }
        }
        break;
    default:
        s->regs[offset / 4] = value;
        break;
    }
    cvp_irq(s);
    return MEMTX_OK;
}

static const MemoryRegionOps cvp_ops = {
    .read_with_attrs = cvp_read,
    .write_with_attrs = cvp_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4 },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
};

static void cvp_reset(Object *obj, ResetType type)
{
    Z62CvpControl *s = Z62_CVP_CONTROL(obj);
    timer_del(s->timer);
    timer_del(s->setup_timer);
    timer_del(s->compute_timer);
    s->compute_pending = false;
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->rx, 0, sizeof(s->rx));
    memset(s->tx, 0, sizeof(s->tx));
    s->rx_head = s->rx_count = s->rx_cursor = s->tx_count = 0;
    s->boot_pending = s->reset_asserted = false;
    s->setup_pending = false;
    s->setup_descriptor = s->setup_opaque = 0;
    s->setup_completions = 0;
    s->boot_completions = s->commands = s->unsupported = 0;
    cvp_irq(s);
}

static int cvp_post_load(void *opaque, int version_id)
{
    Z62CvpControl *s = opaque;
    if (s->rx_head >= CVP_RX_WORDS || s->rx_count > CVP_RX_WORDS ||
        s->rx_cursor > MIN(3u, s->rx_count) || s->tx_count > 3) {
        return -EINVAL;
    }
    cvp_irq(s);
    return 0;
}

static const VMStateDescription cvp_vmstate = {
    .name = TYPE_Z62_CVP_CONTROL, .version_id = 1, .minimum_version_id = 1,
    .post_load = cvp_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, Z62CvpControl, 0x98 / 4),
        VMSTATE_UINT32_ARRAY(rx, Z62CvpControl, CVP_RX_WORDS),
        VMSTATE_UINT32_ARRAY(tx, Z62CvpControl, 3),
        VMSTATE_UINT32(rx_head, Z62CvpControl),
        VMSTATE_UINT32(rx_count, Z62CvpControl),
        VMSTATE_UINT32(rx_cursor, Z62CvpControl),
        VMSTATE_UINT32(tx_count, Z62CvpControl),
        VMSTATE_BOOL(boot_pending, Z62CvpControl),
        VMSTATE_BOOL(reset_asserted, Z62CvpControl),
        VMSTATE_BOOL(setup_pending, Z62CvpControl),
        VMSTATE_UINT32(setup_descriptor, Z62CvpControl),
        VMSTATE_UINT32(setup_opaque, Z62CvpControl),
        VMSTATE_UINT64(boot_completions, Z62CvpControl),
        VMSTATE_UINT64(commands, Z62CvpControl),
        VMSTATE_UINT64(unsupported, Z62CvpControl),
        VMSTATE_UINT64(setup_completions, Z62CvpControl),
        VMSTATE_BOOL(compute_pending, Z62CvpControl),
        VMSTATE_UINT32(compute_op, Z62CvpControl),
        VMSTATE_UINT32(compute_a1, Z62CvpControl),
        VMSTATE_UINT32(compute_a2, Z62CvpControl),
        VMSTATE_UINT64(compute_requests, Z62CvpControl),
        VMSTATE_UINT64(compute_completions, Z62CvpControl),
        VMSTATE_UINT64(compute_dropped, Z62CvpControl),
        VMSTATE_TIMER_PTR(timer, Z62CvpControl),
        VMSTATE_TIMER_PTR(setup_timer, Z62CvpControl),
        VMSTATE_TIMER_PTR(compute_timer, Z62CvpControl),
        VMSTATE_END_OF_LIST()
    },
};

static void cvp_init(Object *obj)
{
    Z62CvpControl *s = Z62_CVP_CONTROL(obj);
    memory_region_init_io(&s->iomem, obj, &cvp_ops, s, TYPE_Z62_CVP_CONTROL, 0x98);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cvp_boot_complete, s);
    s->setup_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cvp_setup_complete, s);
    s->compute_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cvp_compute_complete, s);
    object_property_add_uint64_ptr(obj, "boot-completions", &s->boot_completions, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "commands", &s->commands, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "unsupported-commands", &s->unsupported, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "manager-setup-completions", &s->setup_completions, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "compute-requests",
                                   &s->compute_requests, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "compute-completions",
                                   &s->compute_completions, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "compute-dropped",
                                   &s->compute_dropped, OBJ_PROP_FLAG_READ);
}

static void cvp_finalize(Object *obj)
{
    timer_free(Z62_CVP_CONTROL(obj)->timer);
    timer_free(Z62_CVP_CONTROL(obj)->setup_timer);
    timer_free(Z62_CVP_CONTROL(obj)->compute_timer);
}

static void cvp_realize(DeviceState *dev, Error **errp)
{
    Z62CvpControl *s = Z62_CVP_CONTROL(dev);
    if (!s->root) {
        error_setg(errp, "CVP control requires an engine address space");
        return;
    }
    address_space_init(&s->memory, s->root, TYPE_Z62_CVP_CONTROL);
}

static void cvp_unrealize(DeviceState *dev)
{
    address_space_destroy(&Z62_CVP_CONTROL(dev)->memory);
}

static void cvp_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    static const Property properties[] = {
        DEFINE_PROP_BOOL("laboratory-handshake", Z62CvpControl, laboratory_handshake, false),
        DEFINE_PROP_BOOL("laboratory-manager-setup", Z62CvpControl, laboratory_manager_setup, false),
        DEFINE_PROP_LINK("root", Z62CvpControl, root, TYPE_MEMORY_REGION, MemoryRegion *),
        DEFINE_PROP_BOOL("trace", Z62CvpControl, trace, false),
        DEFINE_PROP_UINT64("boot-delay-ns", Z62CvpControl, delay_ns, 1000),
        DEFINE_PROP_BOOL("laboratory-compute", Z62CvpControl,
                         laboratory_compute, false),
        DEFINE_PROP_UINT64("compute-delay-ns", Z62CvpControl,
                           compute_delay_ns, 2000000),
    };
    device_class_set_props(dc, properties);
    dc->vmsd = &cvp_vmstate;
    dc->user_creatable = false;
    dc->realize = cvp_realize;
    dc->unrealize = cvp_unrealize;
    RESETTABLE_CLASS(oc)->phases.hold = cvp_reset;
}

static void cvp_register_types(void)
{
    static const TypeInfo info = {
        .name = TYPE_Z62_CVP_CONTROL, .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(Z62CvpControl), .instance_init = cvp_init,
        .instance_finalize = cvp_finalize, .class_init = cvp_class_init,
    };
    type_register_static(&info);
}
type_init(cvp_register_types)
