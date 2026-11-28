#!/usr/bin/env bash
# Installe l'ISO sur un disque virtuel jetable (QEMU + OVMF), retire le média,
# redémarre et vérifie la persistance (ADR-0004 §7).
#
# Usage :
#   ./iso/test.sh [chemin.iso]
# Variables :
#   RACKLOOM_ISO_TEST_DIR  répertoire de travail (défaut : /tmp/rackloom-iso-test)
#   RACKLOOM_ISO_TEST_MEM  Mo RAM QEMU (défaut : 4096)
#   RACKLOOM_ISO_TEST_DISK Go disque (défaut : 40)
#   RACKLOOM_ISO_TEST_TIMEOUT secondes max d'installation (défaut : 3600)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=versions.env
source "$ROOT/iso/versions.env"

need() { command -v "$1" >/dev/null 2>&1 || { echo "outil requis absent: $1" >&2; exit 1; }; }
need qemu-system-x86_64
need qemu-img
need xorriso

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
  echo "ISO absente. Lancez d'abord : make iso  (ou ./iso/build.sh --test-seed)" >&2
  exit 1
}

TEST_DIR="${RACKLOOM_ISO_TEST_DIR:-/tmp/rackloom-iso-test}"
MEM="${RACKLOOM_ISO_TEST_MEM:-4096}"
DISK_G="${RACKLOOM_ISO_TEST_DISK:-40}"
TIMEOUT="${RACKLOOM_ISO_TEST_TIMEOUT:-3600}"
mkdir -p "$TEST_DIR"
DISK="$TEST_DIR/disk.qcow2"
VARS="$TEST_DIR/OVMF_VARS.fd"
SERIAL_LOG="$TEST_DIR/serial.log"
MARKER_FILE="$TEST_DIR/marker-found"

say() { printf '\n== %s\n' "$*"; }

say "préparation disque et firmware UEFI"
rm -f "$DISK" "$VARS" "$SERIAL_LOG" "$MARKER_FILE"
qemu-img create -f qcow2 "$DISK" "${DISK_G}G"
cp "$OVMF_VARS_TEMPLATE" "$VARS"

# Si l'ISO n'est pas une graine test, en fabriquer une copie temporaire.
WORK_ISO="$ISO"
SEED_PROBE="$TEST_DIR/user-data.probe"
rm -f "$SEED_PROBE"
xorriso -osirrox on -indev "$ISO" -extract /nocloud/user-data "$SEED_PROBE" >/dev/null 2>&1 || true
if ! grep -Eq 'rackloom-test|iso-test-installed' "$SEED_PROBE" 2>/dev/null; then
  say "génération d'une copie ISO à graine test"
  WORK_ISO="$TEST_DIR/appliance-test.iso"
  TMP_EXTRACT="$TEST_DIR/extract"
  rm -rf "$TMP_EXTRACT"
  mkdir -p "$TMP_EXTRACT"
  xorriso -osirrox on -indev "$ISO" -extract / "$TMP_EXTRACT"
  chmod -R u+w "$TMP_EXTRACT"
  cp "$ROOT/iso/autoinstall/user-data.test" "$TMP_EXTRACT/nocloud/user-data"
  cp "$ROOT/iso/autoinstall/meta-data" "$TMP_EXTRACT/nocloud/meta-data"
  MBR="$TEST_DIR/isohdpfx.bin"
  dd if="$ISO" bs=1 count=432 of="$MBR" status=none
  xorriso -as mkisofs -r -V "RackloomTest" -o "$WORK_ISO" \
    -J -joliet-long --grub2-mbr "$MBR" -partition_offset 16 \
    -b boot/grub/i386-pc/eltorito.img -c boot.catalog \
    -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info \
    -eltorito-alt-boot -e EFI/boot/bootx64.efi -no-emul-boot \
    -isohybrid-gpt-basdat "$TMP_EXTRACT"
fi

say "phase 1 : installation depuis l'ISO (timeout ${TIMEOUT}s)"
# Accélère l'install en injectant FQDN non interactif via smbios (firstboot lit RACKLOOM_FQDN)
# KVM si disponible (beaucoup plus rapide que TCG).
QEMU_ACCEL=tcg
QEMU_CPU=max
if [ -r /dev/kvm ] && [ "${RACKLOOM_ISO_TEST_TCG:-0}" != 1 ]; then
  QEMU_ACCEL=kvm
  QEMU_CPU=host
fi
say "accélération QEMU : $QEMU_ACCEL"

qemu-system-x86_64 \
  -name rackloom-iso-test \
  -machine q35,accel="$QEMU_ACCEL" \
  -cpu "$QEMU_CPU" \
  -m "$MEM" \
  -smp 2 \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
  -drive if=pflash,format=raw,file="$VARS" \
  -drive file="$DISK",if=virtio,format=qcow2,cache=unsafe \
  -cdrom "$WORK_ISO" \
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
    # QEMU s'est arrêté (shutdown: reboot de autoinstall peut tuer selon config)
    break
  fi
  if grep -Eiq 'reboot:|Reached target|cloud-init.*finished|firstboot|login:' "$SERIAL_LOG" 2>/dev/null; then
    # Laisser un peu de marge puis arrêter pour retirer l'ISO
    sleep 15
    installed=1
    break
  fi
  sleep 10
done

if [ "$installed" != 1 ] && kill -0 "$QEMU_PID" 2>/dev/null; then
  # Si toujours vivant après signes d'install partielle, tenter quand même le reboot sans ISO
  if grep -Eiq 'curtin|installing|finish:|success' "$SERIAL_LOG" 2>/dev/null; then
    installed=1
  fi
fi

kill "$QEMU_PID" 2>/dev/null || true
wait "$QEMU_PID" 2>/dev/null || true
trap - EXIT

if [ "$installed" != 1 ]; then
  echo "installation non confirmée dans le délai ; voir $SERIAL_LOG" >&2
  tail -100 "$SERIAL_LOG" >&2 || true
  exit 1
fi

say "phase 2 : redémarrage sans ISO (persistance)"
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
  if grep -Eiq 'rackloom-firstboot|Rackloom appliance|login:|Ubuntu 24.04' "$SERIAL_LOG" 2>/dev/null; then
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
  tail -100 "$SERIAL_LOG" >&2 || true
  exit 1
fi

say "test-iso OK"
echo "disk=$DISK"
echo "serial_log=$SERIAL_LOG"
echo "iso=$ISO"
exit 0
