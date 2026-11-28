#!/usr/bin/env bash
# Étape 2b — mmdebstrap Ubuntu 24.04 (noble) dans la cible.
# Pas de copie live FS → disque.
# shellcheck shell=bash
set -euo pipefail

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SELF_DIR/lib.sh"

TARGET_ROOT=${RACKLOOM_TARGET_ROOT:-/mnt/rackloom-target}
MIRROR=${RACKLOOM_APT_MIRROR:-http://archive.ubuntu.com/ubuntu}
INCLUDE=${RACKLOOM_BOOTSTRAP_INCLUDE:-linux-image-generic,grub-efi-amd64,shim-signed,grub-efi-amd64-signed,systemd,systemd-sysv,sudo,openssh-server,ca-certificates,curl,netplan.io,fdisk,e2fsprogs,locales,kbd}

[ -d "$TARGET_ROOT" ] || die "cible non montée: $TARGET_ROOT"
mountpoint -q "$TARGET_ROOT" || die "cible non montée: $TARGET_ROOT"

if have mmdebstrap; then
  log "mmdebstrap noble → $TARGET_ROOT"
  mmdebstrap \
    --arch=amd64 \
    --variant=minbase \
    --components=main,universe \
    --include="$INCLUDE" \
    --skip=check/empty \
    noble \
    "$TARGET_ROOT" \
    "$MIRROR"
elif have debootstrap; then
  log "debootstrap noble → $TARGET_ROOT (repli)"
  debootstrap --arch=amd64 noble "$TARGET_ROOT" "$MIRROR"
  chroot "$TARGET_ROOT" apt-get update
  # shellcheck disable=SC2086
  chroot "$TARGET_ROOT" apt-get install -y ${INCLUDE//,/ }
else
  die "mmdebstrap ou debootstrap requis"
fi

# fstab
ESP=$(cat "${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}/esp")
ROOT=$(cat "${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}/root")
ESP_UUID=$(blkid -s UUID -o value "$ESP")
ROOT_UUID=$(blkid -s UUID -o value "$ROOT")
cat >"$TARGET_ROOT/etc/fstab" <<EOF
UUID=$ROOT_UUID / ext4 defaults 0 1
UUID=$ESP_UUID /boot/efi vfat umask=0077 0 1
EOF

# Hostname provisoire
printf 'rackloom\n' >"$TARGET_ROOT/etc/hostname"
cat >"$TARGET_ROOT/etc/hosts" <<'EOF'
127.0.0.1 localhost
10.20.0.5 rackloom
EOF

# Marqueur d'étape
mkdir -p "$TARGET_ROOT/etc/rackloom" "$TARGET_ROOT/var/lib/rackloom"
printf '2\n' >"$TARGET_ROOT/etc/rackloom/install-stage"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$TARGET_ROOT/var/lib/rackloom/bootstrap.done"

log "bootstrap terminé"
