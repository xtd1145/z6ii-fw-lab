#!/usr/bin/env bash
# Fetch Ghidra 12.2 + Amazon Corretto 21 for the mas/sla disassembly step.
# GitHub direct is unreachable from this box, so the Ghidra zip goes through gh-proxy.com.
set -u
cd /d/z6ii_fw_lab/tools/dl || exit 1

GHIDRA_URL="https://gh-proxy.com/https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.2_build/ghidra_12.2_PUBLIC_20261016.zip"
GHIDRA_SIZE=569712708
JDK_URL="https://corretto.aws/downloads/resources/21.0.13.0.10/amazon-corretto-21.0.13.0.10-windows-x64-jdk.zip"
JDK_SIZE=202668246

fetch() {  # fetch <url> <out> <expected_bytes>
  local url=$1 out=$2 want=$3
  if [ -f "$out" ] && [ "$(stat -c%s "$out")" = "$want" ]; then
    echo "already complete: $out"; return 0
  fi
  echo "downloading $out (want $want bytes)"
  for attempt in 1 2 3; do
    curl -L --fail --retry 2 --max-time 1800 -o "$out.part" "$url" && {
      mv -f "$out.part" "$out"
      break
    }
    echo "attempt $attempt failed for $out, retrying"; sleep 5
  done
  local got=$(stat -c%s "$out" 2>/dev/null || echo 0)
  if [ "$got" = "$want" ]; then echo "OK $out $got"; else echo "SIZE MISMATCH $out got=$got want=$want"; return 1; fi
}

fetch "$GHIDRA_URL" ghidra_12.2.zip "$GHIDRA_SIZE"; R1=$?
fetch "$JDK_URL"    corretto21.zip   "$JDK_SIZE";    R2=$?

echo "ghidra_rc=$R1 jdk_rc=$R2"
ls -la
exit $((R1 + R2))
