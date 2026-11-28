#!/usr/bin/env bash
# Étape 2a — partitionnement GPT UEFI (ESP + root). Pas de Subiquity/Curtin.
# shellcheck shell=bash
set -euo pipefail

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SELF_DIR/lib.sh"

DISK=${1:?disque requis}
TARGET_ROOT=${RACKLOOM_TARGET_ROOT:-/mnt/rackloom-target}

have sgdisk || die "sgdisk (gdisk) requis"
have mkfs.vfat || die "mkfs.vfat requis"
have mkfs.ext4 || die "mkfs.ext4 requis"

log "partitionnement GPT de $DISK (ESP 512MiB + root)"

# Démontages éventuels
umount -R "$TARGET_ROOT" 2>/dev/null || true
for p in "${DISK}"*; do
  umount "$p" 2>/dev/null || true
done

sgdisk --zap-all "$DISK"
sgdisk -n 1:0:+512M -t 1:ef00 -c 1:"EFI System" "$DISK"
sgdisk -n 2:0:0 -t 2:8300 -c 2:"Rackloom root" "$DISK"
sgdisk -p "$DISK" >/dev/null

# Attendre les nœuds
sleep 1
partprobe "$DISK" 2>/dev/null || true
udevadm settle 2>/dev/null || true

# Noms de partitions : /dev/vda1 vs /dev/nvme0n1p1
if [[ "$DISK" =~ [0-9]$ ]]; then
  P1="${DISK}p1"
  P2="${DISK}p2"
else
  P1="${DISK}1"
  P2="${DISK}2"
fi

[ -b "$P1" ] || die "partition ESP absente: $P1"
[ -b "$P2" ] || die "partition root absente: $P2"

mkfs.vfat -F32 -n RLEFI "$P1"
mkfs.ext4 -F -L rlroot "$P2"

mkdir -p "$TARGET_ROOT"
mount "$P2" "$TARGET_ROOT"
mkdir -p "$TARGET_ROOT/boot/efi"
mount "$P1" "$TARGET_ROOT/boot/efi"

printf '%s\n' "$P1" >"${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}/esp"
printf '%s\n' "$P2" >"${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}/root"
log "monté : $P2 → $TARGET_ROOT , $P1 → $TARGET_ROOT/boot/efi"
