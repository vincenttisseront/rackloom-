#!/usr/bin/env bash
# Étape 2c — GRUB EFI sur le disque cible.
# shellcheck shell=bash
set -euo pipefail

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SELF_DIR/lib.sh"

DISK=${1:?disque requis}
TARGET_ROOT=${RACKLOOM_TARGET_ROOT:-/mnt/rackloom-target}

[ -d "$TARGET_ROOT/boot/efi" ] || die "ESP non montée sous $TARGET_ROOT/boot/efi"

for fs in proc sys dev dev/pts run; do
  mkdir -p "$TARGET_ROOT/$fs"
  mount --bind "/$fs" "$TARGET_ROOT/$fs" 2>/dev/null || true
done

log "installation GRUB EFI sur $DISK"
chroot "$TARGET_ROOT" grub-install \
  --target=x86_64-efi \
  --efi-directory=/boot/efi \
  --bootloader-id=Rackloom \
  --recheck \
  "$DISK" || die "grub-install a échoué"

chroot "$TARGET_ROOT" update-grub || true

# Unité + message « Installation Rackloom terminée »
cat >"$TARGET_ROOT/etc/systemd/system/rackloom-install-done.service" <<'EOF'
[Unit]
Description=Rackloom install complete banner
After=multi-user.target
ConditionPathExists=!/var/lib/rackloom/install-done.shown

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rackloom-install-done
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

cat >"$TARGET_ROOT/usr/local/sbin/rackloom-install-done" <<'EOF'
#!/bin/bash
set -euo pipefail
MSG="Installation Rackloom terminée"
{
  printf '\n========================================\n'
  printf '  %s\n' "$MSG"
  printf '========================================\n\n'
} > /dev/console 2>/dev/null || true
printf '%s\n' "$MSG" > /etc/motd
printf '%s\n\n' "$MSG" > /etc/issue
mkdir -p /var/lib/rackloom
date -u +%Y-%m-%dT%H:%M:%SZ > /var/lib/rackloom/install-done.shown
EOF
chmod 0755 "$TARGET_ROOT/usr/local/sbin/rackloom-install-done"
chroot "$TARGET_ROOT" systemctl enable rackloom-install-done.service

printf '%s\n' "Installation Rackloom terminée" >"$TARGET_ROOT/etc/motd"

for fs in run dev/pts dev sys proc; do
  umount "$TARGET_ROOT/$fs" 2>/dev/null || true
done

log "GRUB installé"
