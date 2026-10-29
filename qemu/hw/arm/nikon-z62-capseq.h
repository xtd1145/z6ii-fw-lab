/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Adapted from Fly-beep-256/z62_qemu, commit
 * 1a79b9dbdc7bef89f942baf0335b80cb6ba47e71,
 * src/qemu/hw/arm/nikon-z6ii-capseq-actuator.h.
 * Modified 2026-10-28: local BootConfig/QOM wiring, bounded passive trace,
 * explicit delay, default-off actuator and timeout control. Preserve the
 * upstream request-rise / request-fall-cancel assumption. This is not a
 * recovered mechanical shutter, latency or electrical polarity model.
 * Included after NikonZ72BootConfig and controller_update_irqs. */

static bool capseq_active(NikonZ72BootConfig *s)
{
    return s->master_role && s->laboratory_capseq_actuator;
}

static void capseq_trace(NikonZ72BootConfig *s, const char *event,
                          hwaddr address, uint32_t value)
{
    if (!s->master_role ||
        !(s->laboratory_capseq_trace || s->laboratory_capseq_actuator)) {
        return;
    }
    if (s->capseq_trace_count++ >= 256) {
        return;
    }
    fprintf(stderr, "Z62_CAPSEQ event=%s address=%08" HWADDR_PRIx
        " value=%08x pc=%08x virtual_ns=%" PRId64
        " request=%u armed=%u done=%u generation=%" PRIu64
        " delay_ns=%" PRIu64 " actuator=%u timing_verified=0\n",
        event, address, value,
        current_cpu ? ARM_CPU(current_cpu)->env.regs[15] : 0,
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
        s->capseq_request, s->capseq_armed, s->capseq_done,
        s->capseq_generation, s->capseq_delay_ns,
        s->laboratory_capseq_actuator);
}

static void capseq_complete(void *opaque)
{
    NikonZ72BootConfig *s = opaque;
    if (!capseq_active(s) || !s->capseq_armed || !s->capseq_request ||
        s->capseq_armed_generation != s->capseq_generation) {
        return;
    }
    s->capseq_armed = false;
    s->capseq_done = true;
    s->capseq_completions++;
    *boot_config_value(s, 0x0426f008) |= 1;
    capseq_trace(s, "complete", 0x04282014, 0x80);
    controller_update_irqs(s);
}

static void capseq_output(NikonZ72BootConfig *s, hwaddr address,
                           uint32_t value)
{
    if (!s->master_role || address != 0x04282018 || !(value & 0x01000000) ||
        !(s->laboratory_capseq_actuator || s->laboratory_capseq_trace)) {
        return;
    }
    bool request = (value & 0x100) != 0;
    if (request == s->capseq_request) {
        return;
    }
    s->capseq_request = request;
    s->capseq_generation++;
    timer_del(s->capseq_timer);
    if (s->capseq_armed) {
        s->capseq_canceled++;
    }
    s->capseq_armed = false;
    s->capseq_done = false;
    if (!request) {
        capseq_trace(s, "request-fall", address, value);
        return; /* Latched pending survives input falling until native W1C. */
    }
    s->capseq_requests++;
    s->capseq_armed_generation = s->capseq_generation;
    if (capseq_active(s) && !s->capseq_timeout) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (now > INT64_MAX - s->capseq_delay_ns) {
            capseq_trace(s, "deadline-overflow", address, value);
            return;
        }
        s->capseq_armed = true;
        timer_mod(s->capseq_timer, now + s->capseq_delay_ns);
    }
    capseq_trace(s, "request-rise", address, value);
}
