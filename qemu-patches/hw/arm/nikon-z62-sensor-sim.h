/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional IMX410-style frame source for the Z6 II laboratory board. */
#ifndef NIKON_Z62_SENSOR_SIM_H
#define NIKON_Z62_SENSOR_SIM_H

#include "qom/object.h"
#include <stdbool.h>
#include <stdint.h>

#define TYPE_Z62_SENSOR_SIM "nikon-z62-sensor-sim"
typedef struct Z62SensorSim Z62SensorSim;
DECLARE_INSTANCE_CHECKER(Z62SensorSim, Z62_SENSOR_SIM, TYPE_Z62_SENSOR_SIM)

/* Render (or return the cached) frame into host_rgba and, when guest_address
 * is non-zero, into the engine memory view. Returns false when disabled. */
bool z62_sensor_sim_frame(Z62SensorSim *s, uint32_t guest_address,
                          uint8_t *host_rgba, unsigned w, unsigned h,
                          int64_t now_ns);

/* Functional still capture: render a Bayer RGGB still and a demosaiced PPM
 * at the configured capture size. Files are written only when a capture
 * prefix is configured. Not the firmware's encoder or card path. */
bool z62_sensor_sim_capture(Z62SensorSim *s, uint64_t exposure_ns,
                            bool electronic);

/* Write a Bayer RGGB still straight into the firmware's own physical capture
 * buffer (scatter record 10034070: PA 0x80800000, 0x2313880 bytes - the
 * extent matches a 12-bit packed 6048x4024 frame). Samples are 16-bit
 * little-endian, value range 0..16320 as produced by the existing capture
 * path. This is the bridge that lets the firmware's CaptureService read real
 * pixels instead of a zero-filled buffer; nothing is written to host files. */
bool z62_sensor_sim_fill_capture(Z62SensorSim *s, uint32_t guest_address,
                                 uint64_t exposure_ns);

/* Fill the live-view scan-out planes from the same scene function.
 *
 * With real NOR parameters the firmware enters its normal live-view path and
 * programs the LCD video window to YUV422 planar: Y at 0xdcf33000 (stride
 * 0x400) and an interleaved U/V plane at 0xdcfdd800. Nothing ever wrote those
 * planes in the model, so the panel stayed black even though every display
 * register was correct. This renders the scene into those planes.
 *
 * Layout assumed: Y plane is one byte per pixel per row (y_stride bytes), the
 * UV plane is U,V interleaved and also y_stride bytes per row (two bytes per
 * pixel pair). That is what the programmed strides imply for 1024x682. */
bool z62_sensor_sim_fill_yuv(Z62SensorSim *s, uint32_t y_address,
                             uint32_t uv_address, unsigned w, unsigned h,
                             unsigned y_stride, unsigned uv_stride);

#endif
