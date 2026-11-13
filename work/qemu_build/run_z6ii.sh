#!/usr/bin/env bash
# Run the nikon-z6ii machine against the decrypted eg master module.
#
# -d unimp is the point of the exercise: every XN window the firmware's own
# page table declares is an unimplemented-device, so the log accumulates the
# addresses the boot code actually pokes. That trace becomes the peripheral
# model. -d in_asm gives real A32 disassembly (capstone was enabled at build
# time) so a crash can be read straight off the log.
set -uo pipefail
export PATH="/ucrt64/bin:/usr/bin:/bin:$PATH"
Q=/d/z6ii_fw_lab/tools/src/qemu/build/qemu-system-arm.exe
MAS=/d/z6ii_fw_lab/extracted/eg1850_mas_01700
OUT=/d/z6ii_fw_lab/work/qemu_build/run
mkdir -p "$OUT"

# SIGKILL, not the default TERM/SIGINT: mas ends up in a tight loop and QEMU's
# main loop does not act on a signal while a TCG thread is spinning, so a
# --signal=INT timeout leaves a qemu-system-arm writing gigabytes after the
# script "finished". Buffered log loss on SIGKILL is one FILE* buffer, which is
# nothing next to a trace this size.
timeout --signal=KILL "${Z6II_TIMEOUT:-20}" "$Q" \
  -M nikon-z6ii -cpu cortex-a9 -smp 2 \
  -kernel "$MAS" \
  -d "${Z6II_LOG:-unimp,int}" -D "$OUT/trace.log" \
  -monitor none -display none \
  2> "$OUT/stderr.log"
rc=$?

echo "qemu rc=$rc (137 = hit the run-time limit, which is expected for a spin loop)"
echo "trace : $(wc -l < "$OUT/trace.log") lines, $(du -h "$OUT/trace.log" | cut -f1)"
echo "stderr:"; tail -5 "$OUT/stderr.log"

echo
echo "=== first 40 log lines (the reset path) ==="
head -40 "$OUT/trace.log"

echo
echo "=== exception kinds, by count ==="
grep -aoE "(Take (data|prefetch) abort|exception|Unlock unsupported|unimplemented device (read|write))" \
  "$OUT/trace.log" | sort | uniq -c | sort -rn | head
