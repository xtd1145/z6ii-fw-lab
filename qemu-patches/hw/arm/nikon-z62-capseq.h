/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Adapted from Fly-beep-256/z62_qemu, commit
 * 1a79b9dbdc7bef89f942baf0335b80cb6ba47e71,
 * src/qemu/hw/arm/nikon-z6ii-capseq-actuator.h.
 * Modified 2026-10-28: local BootConfig/QOM wiring, bounded passive trace,
 * explicit delay, default-off actuator and timeout control. Preserve the
 * upstream request-rise / request-fall-cancel assumption.
 * Modified 2026-11-09: functional focal-plane shutter timing. A request rises
 * a mechanical two-curtain cycle (travel, exposure, travel, readout) or an
 * electronic exposure + rolling readout. These durations are explicit lab
 * parameters, not recovered silicon timing; pixel output is produced by the
 * sensor model, not here.
 * Included after NikonZ72BootConfig and controller_update_irqs. */

enum {
    Z62_SHUTTER_IDLE = 0,
    Z62_SHUTTER_FIRST,
    Z62_SHUTTER_EXPOSE,
    Z62_SHUTTER_SECOND,
    Z62_SHUTTER_READOUT,
    Z62_SHUTTER_COMPLETE,
};

/* Physical capture buffer of the master engine: scatter record 10034070
 * carries PA 80800000 / 0x2313880. This is where the sensor readout lands on
 * real silicon, so it is also where a simulated frame has to appear for the
 * firmware's CaptureService to see pixels instead of zeros. */
#define Z62_CAPTURE_BUFFER_PA   0x80800000

static bool capseq_active(NikonZ72BootConfig *s)
{
    return s->master_role &&
           (s->laboratory_capseq_actuator ||
            (s->laboratory_shutter_hardware && s->shutter_armed));
}

static void capseq_trace(NikonZ72BootConfig *s, const char *event,
                          hwaddr address, uint32_t value)
{
    if (!s->master_role ||
        !(s->laboratory_capseq_trace || s->laboratory_capseq_actuator ||
          (s->laboratory_shutter_hardware && s->shutter_armed))) {
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

static void shutter_trace(NikonZ72BootConfig *s, const char *event)
{
    if (!s->master_role || !s->laboratory_shutter_hardware) {
        return;
    }
    fprintf(stderr, "Z62_SHUTTER event=%s state=%u electronic=%u"
            " exposure_ns=%" PRIu64 " travel_ns=%" PRIu64
            " readout_ns=%" PRIu64 " virtual_ns=%" PRId64
            " completions=%" PRIu64 " canceled=%" PRIu64 "\n",
            event, s->shutter_state, s->shutter_electronic,
            s->shutter_last_exposure_ns, s->shutter_travel_ns,
            s->shutter_readout_ns, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
            s->shutter_completions, s->shutter_canceled);
}

static void capture_readout_done(void *opaque)
{
    NikonZ72BootConfig *s = opaque;

    s->capture_readout_pending = false;
    if (s->sensor_sim) {
        z62_sensor_sim_fill_capture(s->sensor_sim, Z62_CAPTURE_BUFFER_PA,
                                    s->capseq_delay_ns);
    }
    s->capture_readout_done_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* The frame-end edge is the same decoded sensor line the periodic
     * scan-out raises; a configured sensor path turns it into 0x1508 bit1
     * (IRQ92). This is the only "pixels are valid" edge the model can offer,
     * and it now sits after the readout instead of at the shutter event. */
    qemu_set_irq(s->sensor_frame_end_irq, 1);
    qemu_set_irq(s->sensor_frame_end_irq, 0);
    fprintf(stderr, "Z62_READOUT state=done readout_ns=%" PRIu64
            " virtual_ns=%" PRId64 " frame_end_pulse=1\n",
            s->capture_readout_ns, s->capture_readout_done_ns);
}

/* Actuator path: the curtain closes at capseq_complete, then the sensor
 * clocks the frame out over capture_readout_ns. Pixels and the frame-ready
 * edge land at the end of that window, not at the shutter event. */
static void capture_readout_start(NikonZ72BootConfig *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->capture_readout_ns) {
        capture_readout_done(s);
        return;
    }
    s->capture_readout_pending = true;
    fprintf(stderr, "Z62_READOUT state=start readout_ns=%" PRIu64
            " virtual_ns=%" PRId64 "\n", s->capture_readout_ns, now);
    timer_mod(s->capture_readout_timer, now + s->capture_readout_ns);
}

static void capseq_complete(NikonZ72BootConfig *s)
{
    s->capseq_armed = false;
    s->capseq_done = true;
    s->capseq_completions++;
    /* Both completion routes land here: the mechanical/electronic curtain
     * cycle (which already filled the buffer at its exposure point) and the
     * plain laboratory actuator. */
    *boot_config_value(s, 0x0426f008) |= 1;
    capseq_trace(s, "complete", 0x04282014, 0x80);
    controller_update_irqs(s);
    if (s->sensor_sim && !s->laboratory_shutter_hardware) {
        capture_readout_start(s);
    }
}

static void shutter_tick(void *opaque);

static void shutter_start(NikonZ72BootConfig *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->shutter_busy) {
        s->shutter_canceled++;
        shutter_trace(s, "restart");
    }
    s->shutter_busy = true;
    s->shutter_start_ns = now;
    s->shutter_last_exposure_ns = s->shutter_exposure_ns;
    s->shutter_state = s->shutter_electronic ? Z62_SHUTTER_EXPOSE :
                                              Z62_SHUTTER_FIRST;
    s->capseq_armed = true;
    shutter_trace(s, "start");
    timer_mod(s->capseq_timer, now +
              (s->shutter_state == Z62_SHUTTER_EXPOSE ?
               s->shutter_exposure_ns : s->shutter_travel_ns));
}

static void shutter_sensor_capture(NikonZ72BootConfig *s)
{
    if (s->sensor_sim) {
        z62_sensor_sim_capture(s->sensor_sim, s->shutter_last_exposure_ns,
                               s->shutter_electronic);
        z62_sensor_sim_fill_capture(s->sensor_sim, Z62_CAPTURE_BUFFER_PA,
                                    s->shutter_last_exposure_ns);
    }
}

static void shutter_tick(void *opaque)
{
    NikonZ72BootConfig *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!s->laboratory_shutter_hardware || !s->shutter_busy) {
        return;
    }
    switch (s->shutter_state) {
    case Z62_SHUTTER_FIRST:
        s->shutter_state = Z62_SHUTTER_EXPOSE;
        shutter_trace(s, "open");
        timer_mod(s->capseq_timer, now + s->shutter_exposure_ns);
        break;
    case Z62_SHUTTER_EXPOSE:
        shutter_sensor_capture(s);
        if (s->shutter_electronic) {
            s->shutter_state = Z62_SHUTTER_READOUT;
            shutter_trace(s, "readout");
            timer_mod(s->capseq_timer, now + s->shutter_readout_ns);
        } else {
            s->shutter_state = Z62_SHUTTER_SECOND;
            shutter_trace(s, "close");
            timer_mod(s->capseq_timer, now + s->shutter_travel_ns);
        }
        break;
    case Z62_SHUTTER_SECOND:
        s->shutter_state = Z62_SHUTTER_READOUT;
        shutter_trace(s, "readout");
        timer_mod(s->capseq_timer, now + s->shutter_readout_ns);
        break;
    case Z62_SHUTTER_READOUT:
        s->shutter_state = Z62_SHUTTER_COMPLETE;
        s->shutter_busy = false;
        s->shutter_completions++;
        shutter_trace(s, "complete");
        capseq_complete(s);
        break;
    default:
        s->shutter_busy = false;
        break;
    }
}

static void capseq_tick(void *opaque)
{
    NikonZ72BootConfig *s = opaque;
    if (s->laboratory_shutter_hardware && s->shutter_busy) {
        shutter_tick(s);
        return;
    }
    if (!capseq_active(s) || !s->capseq_armed ||
        s->capseq_armed_generation != s->capseq_generation) {
        return;
    }
    capseq_complete(s);
}

static void capseq_output(NikonZ72BootConfig *s, hwaddr address,
                           uint32_t value)
{
    if (!s->master_role || address != 0x04282018 || !(value & 0x01000000) ||
        !(s->laboratory_capseq_actuator || s->laboratory_capseq_trace ||
          (s->laboratory_shutter_hardware && s->shutter_armed))) {
        return;
    }
    bool request = (value & 0x100) != 0;
    if (request == s->capseq_request) {
        return;
    }
    s->capseq_request = request;
    s->capseq_generation++;
    if (s->laboratory_shutter_hardware && s->shutter_busy) {
        if (!request) {
            /* The curtain cycle is latched; the command pulse may fall early. */
            capseq_trace(s, "request-fall-during-shutter", address, value);
            return;
        }
    } else if (!request && s->capseq_armed) {
        /* A latched capture must survive the command pulse ending. The body
         * takes ~6.1 ms while the pulse lasts ~1.2 ms, so cancelling on the
         * falling edge would drop every real capture and leave the camera on
         * a blank screen - the same signature as the black-screen failures
         * this model exists to reproduce. Only a new request restarts it. */
        s->capseq_armed_generation = s->capseq_generation;
        capseq_trace(s, "request-fall-latched", address, value);
        return;
    } else {
        timer_del(s->capseq_timer);
    }
    if (s->capseq_armed && !(s->laboratory_shutter_hardware && s->shutter_busy)) {
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
    if (s->laboratory_shutter_hardware && s->shutter_armed) {
        shutter_start(s);
    } else if (s->laboratory_capseq_actuator && !s->capseq_timeout) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (now > INT64_MAX - s->capseq_delay_ns) {
            capseq_trace(s, "deadline-overflow", address, value);
            return;
        }
        s->capseq_armed = true;
        timer_mod(s->capseq_timer, now + s->capseq_delay_ns);
    } else if (s->laboratory_shutter_hardware) {
        capseq_trace(s, "shutter-not-armed", address, value);
    }
    capseq_trace(s, "request-rise", address, value);
}
