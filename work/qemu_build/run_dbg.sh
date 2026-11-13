#!/usr/bin/env bash
# Run the machine under the gdbstub and dump the CPU state after it has been
# running for a while.
#
# The boot code's own exception handlers end in "b ." panic loops, so a trace
# tells us *which* exception fired but not the register contents that caused it.
# Attaching gdb lets us read the context block the handler built (sp+0x00..0x30
# is r0..ip, sp+0x34 is the faulting mode's SP and sp+0x38 its LR, i.e. the
# faulting instruction + 8) and the shared SMP block in DDR.
set -uo pipefail
export PATH="/ucrt64/bin:/usr/bin:/bin:$PATH"
Q=/d/z6ii_fw_lab/tools/src/qemu/build/qemu-system-arm.exe
MAS=/d/z6ii_fw_lab/extracted/eg1850_mas_01700
OUT=/d/z6ii_fw_lab/work/qemu_build/run
mkdir -p "$OUT"
WAIT="${Z6II_WAIT:-10}"

"$Q" -M nikon-z6ii -cpu cortex-a9 -smp 2 -kernel "$MAS" \
  -d "${Z6II_LOG:-int,unimp}" -D "$OUT/dbg_trace.log" \
  -monitor none -display none -gdb tcp::"${Z6II_GDBPORT:-1234}" &
QPID=$!
sleep "$WAIT"

gdb -batch -nx \
  -ex "set architecture arm" \
  -ex "target remote localhost:${Z6II_GDBPORT:-1234}" \
  -ex "info threads" \
  -ex "thread 1" -ex "info registers pc sp lr cpsr" \
  -ex "x/17wx \$sp" \
  -ex "thread 2" -ex "info registers pc sp lr cpsr" \
  -ex "x/17wx \$sp" \
  -ex "x/32wx 0xc18ad800" \
  -ex "x/16wx 0xc18ad840" \
  2>&1

kill -9 "$QPID" 2>/dev/null
wait 2>/dev/null
echo "--- last 12 lines of $OUT/dbg_trace.log ---"
tail -12 "$OUT/dbg_trace.log"
