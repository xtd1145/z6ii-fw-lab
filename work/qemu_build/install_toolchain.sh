#!/usr/bin/env bash
# MSYS2 toolchain bootstrap for the nikon-z6ii QEMU lab.
# Ordered the way MSYS2 requires: runtime/bash first, then full sync, then packages.
set -u
export LANG=C
export MSYS2_ARG_CONV_EXCL="*"
export PAGER=cat
# pacman lives in the msys layer; make the script work with or without a login shell.
export PATH="/usr/bin:/bin:/mingw64/bin:/ucrt64/bin:$PATH"
command -v pacman >/dev/null || { echo "FATAL: pacman still not on PATH"; exit 127; }
echo "pacman: $(command -v pacman)"

step() { echo; echo "##### $* #####"; }

step "seed keys (first run only)"
if [ ! -d /etc/pacman.d/gnupg/pubring.kbx ] && [ ! -f /etc/pacman.d/gnupg/pubring.kbx ]; then
  pacman-key --init || true
  pacman-key --populate msys2 || true
fi

step "update core runtime + bash"
pacman -Syu --noconfirm --needed msys2-runtime bash git || true

step "full system sync"
pacman -Syu --noconfirm || true

step "install build + analysis packages"
pacman -S --noconfirm --needed \
  base-devel \
  git \
  mingw-w64-ucrt-x86_64-gcc \
  mingw-w64-ucrt-x86_64-make \
  mingw-w64-ucrt-x86_64-meson \
  mingw-w64-ucrt-x86_64-ninja \
  mingw-w64-ucrt-x86_64-pkgconf \
  mingw-w64-ucrt-x86_64-python \
  mingw-w64-ucrt-x86_64-glib2 \
  mingw-w64-ucrt-x86_64-pixman \
  mingw-w64-ucrt-x86_64-zlib \
  mingw-w64-ucrt-x86_64-zstd \
  mingw-w64-ucrt-x86_64-capstone \
  mingw-w64-ucrt-x86_64-libusb \
  mingw-w64-ucrt-x86_64-SDL2 \
  mingw-w64-ucrt-x86_64-llvm \
  mingw-w64-ucrt-x86_64-arm-none-eabi-binutils \
  mingw-w64-ucrt-x86_64-gdb-multiarch
RC=$?

step "verify"
for t in gcc ld meson ninja pkg-config python capstone dlltool arm-none-eabi-objdump gdb-multiarch clang; do
  printf '%-24s ' "$t"; command -v "$t" >/dev/null && "$t" --version 2>/dev/null | head -1 || echo "NOT FOUND"
done
pkg-config --exists glib-2.0 && echo "glib-2.0: $(pkg-config --modversion glib-2.0)" || echo "glib-2.0: MISSING"
pkg-config --exists pixman-1 && echo "pixman-1: $(pkg-config --modversion pixman-1)" || echo "pixman-1: MISSING"
pkg-config --exists capstone && echo "capstone: $(pkg-config --modversion capstone)" || echo "capstone: MISSING"

echo
echo "##### DONE (pacman rc=$RC) #####"
exit $RC
