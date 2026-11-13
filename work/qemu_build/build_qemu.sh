#!/usr/bin/env bash
# Configure + build qemu-system-arm for the nikon-z6ii lab.
# Why these flags:
#  - --python=/ucrt64/bin/python.exe: native Python driving the native meson
#    shim, with MSYS2 argument conversion left enabled (see below).
#  - arm-softmmu only: the lab needs system emulation, not user-mode.
#  - --enable-fdt=system: arm-softmmu hard-requires FDT (meson.build aborts with
#    "fdt disabled but required by targets arm-softmmu"). We use MSYS2's dtc
#    package via cc.find_library('fdt') instead of the uninitialised dtc
#    submodule, which would need GitHub access.
#  - --disable-werror: MSYS2 ships GCC 16, which warns on code QEMU 11.1 treats
#    as an error; without this the build dies on style-only diagnostics.
#  - capstone on so `-d in_asm` prints real A32 disassembly of firmware code.
set -eu
export PATH="/ucrt64/bin:/usr/bin:/bin:$PATH"
# Do NOT set MSYS2_ARG_CONV_EXCL here: suppressing argument conversion is what
# makes the native Python see "/d/..." instead of "D:/..." and fail to open
# mkvenv.py / meson.
SRC=/d/z6ii_fw_lab/tools/src/qemu
BUILD="$SRC/build"

cd "$SRC"

if [ ! -f "$BUILD/build.ninja" ]; then
  rm -rf "$BUILD"
  echo "##### configure #####"
  ./configure \
    --python=/ucrt64/bin/python.exe \
    --target-list=arm-softmmu \
    --disable-docs \
    --enable-fdt=system \
    --disable-vnc \
    --disable-curses \
    --disable-opengl \
    --disable-werror \
    --enable-capstone \
    --enable-libusb \
    --enable-sdl
fi

echo "##### ninja #####"
# The ninja target carries the .exe suffix on Windows; "qemu-system-arm" alone
# is not a known target there.
# JOBS is capped, not left at ninja's default of one per core: at -j24 the
# concurrent cc1.exe processes exhausted Windows *commit* space (the page-file
# limit, which stays far below the 38 GB of free physical RAM here) and GCC died
# with "out of memory allocating 485416 bytes" on files as small as digic.c.
ninja -j "${JOBS:-8}" -C "$BUILD" qemu-system-arm.exe
echo "##### built #####"
ls -la "$BUILD/qemu-system-arm.exe"
"$BUILD/qemu-system-arm.exe" --version | head -2
