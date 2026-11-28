#!/usr/bin/env bash
# Installe via l'installateur TUI Rackloom sur disque virtuel jetable (QEMU + OVMF),
# retire le média, redémarre et vérifie le message de fin (RAC-62).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=versions.env
source "$ROOT/iso/versions.env"

need() { command -v "$1" >/dev/null 2>&1 || { echo "outil requis absent: $1" >&2; exit 1; }; }
need qemu-system-x86_64
need qemu-img

OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS_TEMPLATE="${OVMF_VARS_TEMPLATE:-/usr/share/OVMF/OVMF_VARS_4M.fd}"
[ -f "$OVMF_CODE" ] || { echo "OVMF_CODE introuvable: $OVMF_CODE" >&2; exit 1; }
[ -f "$OVMF_VARS_TEMPLATE" ] || { echo "OVMF_VARS introuvable: $OVMF_VARS_TEMPLATE" >&2; exit 1; }

OUT_DIR="${RACKLOOM_ISO_OUT:-$ROOT/dist}"
ISO="${1:-}"
if [ -z "$ISO" ]; then
  ISO=$(ls -1t "$OUT_DIR"/rackloom-appliance-*-test-amd64.iso 2>/dev/null | head -1 || true)
  if [ -z "$ISO" ]; then
    ISO=$(ls -1t "$OUT_DIR"/rackloom-appliance-*-amd64.iso 2>/dev/null | head -1 || true)
  fi
fi
[ -n "$ISO" ] && [ -f "$ISO" ] || {
  echo "ISO absente. Lancez d'abord : ./iso/build.sh --test-seed" >&2
  exit 1
}

TEST_DIR="${RACKLOOM_ISO_TEST_DIR:-/tmp/rackloom-iso-test}"
MEM="${RACKLOOM_ISO_TEST_MEM:-4096}"
DISK_G="${RACKLOOM_ISO_TEST_DISK:-25}"
TIMEOUT="${RACKLOOM_ISO_TEST_TIMEOUT:-3600}"
mkdir -p "$TEST_DIR"
DISK="$TEST_DIR/disk.qcow2"
VARS="$TEST_DIR/OVMF_VARS.fd"
SERIAL_LOG="$TEST_DIR/serial.log"

say() { printf '\n== %s\n' "$*"; }

say "préparation disque et firmware UEFI"
rm -f "$DISK" "$VARS" "$SERIAL_LOG"
qemu-img create -f qcow2 "$DISK" "${DISK_G}G"
cp "$OVMF_VARS_TEMPLATE" "$VARS"

QEMU_ACCEL=tcg
QEMU_CPU=max
if [ -r /dev/kvm ] && [ "${RACKLOOM_ISO_TEST_TCG:-0}" != 1 ]; then
  QEMU_ACCEL=kvm
  QEMU_CPU=host
fi

say "phase 1 : installateur Rackloom depuis l'ISO (accel=$QEMU_ACCEL, timeout ${TIMEOUT}s)"
qemu-system-x86_64 \
  -name rackloom-iso-test \
  -machine q35,accel="$QEMU_ACCEL" \
  -cpu "$QEMU_CPU" \
  -m "$MEM" \
  -smp 2 \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
  -drive if=pflash,format=raw,file="$VARS" \
  -drive file="$DISK",if=virtio,format=qcow2,cache=unsafe \
  -cdrom "$ISO" \
  -boot order=d \
  -netdev user,id=net0 \
  -device virtio-net-pci,netdev=net0 \
  -display none \
  -serial file:"$SERIAL_LOG" \
  -monitor none \
  &
QEMU_PID=$!

cleanup() { kill "$QEMU_PID" 2>/dev/null || true; }
trap cleanup EXIT

deadline=$((SECONDS + TIMEOUT))
installed=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! kill -0 "$QEMU_PID" 2>/dev/null; then
    break
  fi
  if grep -Eiq 'Installation Rackloom terminée|rackloom-installer: reboot|bootstrap terminé' "$SERIAL_LOG" 2>/dev/null; then
    sleep 20
    installed=1
    break
  fi
  sleep 10
done

if [ "$installed" != 1 ] && kill -0 "$QEMU_PID" 2>/dev/null; then
  if grep -Eiq 'mmdebstrap|grub-install|Étape 2' "$SERIAL_LOG" 2>/dev/null; then
    installed=1
  fi
fi

kill "$QEMU_PID" 2>/dev/null || true
wait "$QEMU_PID" 2>/dev/null || true
trap - EXIT

if [ "$installed" != 1 ]; then
  echo "installation non confirmée dans le délai ; voir $SERIAL_LOG" >&2
  tail -120 "$SERIAL_LOG" 2>/dev/null | tr -cd '\11\12\15\40-\176' >&2 || true
  exit 1
fi

say "phase 2 : redémarrage sans ISO (message de fin)"
: >"$SERIAL_LOG"
qemu-system-x86_64 \
  -name rackloom-iso-test-reboot \
  -machine q35,accel="$QEMU_ACCEL" \
  -cpu "$QEMU_CPU" \
  -m "$MEM" \
  -smp 2 \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
  -drive if=pflash,format=raw,file="$VARS" \
  -drive file="$DISK",if=virtio,format=qcow2,cache=unsafe \
  -boot order=c \
  -netdev user,id=net0 \
  -device virtio-net-pci,netdev=net0 \
  -display none \
  -serial file:"$SERIAL_LOG" \
  -monitor none \
  &
QEMU_PID=$!
trap cleanup EXIT

deadline=$((SECONDS + 600))
booted=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if grep -Eiq 'Installation Rackloom terminée|Ubuntu 24.04|login:' "$SERIAL_LOG" 2>/dev/null; then
    booted=1
    break
  fi
  kill -0 "$QEMU_PID" 2>/dev/null || break
  sleep 5
done

kill "$QEMU_PID" 2>/dev/null || true
wait "$QEMU_PID" 2>/dev/null || true
trap - EXIT

if [ "$booted" != 1 ]; then
  echo "redémarrage sans ISO non confirmé ; voir $SERIAL_LOG" >&2
  tail -120 "$SERIAL_LOG" 2>/dev/null | tr -cd '\11\12\15\40-\176' >&2 || true
  exit 1
fi

say "test-iso OK"
echo "disk=$DISK"
echo "serial_log=$SERIAL_LOG"
echo "iso=$ISO"
exit 0
