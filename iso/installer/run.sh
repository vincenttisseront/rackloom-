#!/usr/bin/env bash
# Installateur TUI Rackloom — architecture réelle (pas Subiquity / autoinstall UX).
#
# Usage :
#   run.sh                  # interactif (Étape 1–2)
#   run.sh --auto --disk /dev/vda
#   RACKLOOM_INSTALLER_AUTO=1 RACKLOOM_DISK=/dev/vda run.sh
set -euo pipefail

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SELF_DIR/lib.sh"

AUTO=0
DISK_ARG=""
NO_REBOOT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --auto) AUTO=1 ;;
    --disk) DISK_ARG=${2:?}; shift ;;
    --no-reboot) NO_REBOOT=1 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *) die "option inconnue: $1" ;;
  esac
  shift
done

if [ "${RACKLOOM_INSTALLER_AUTO:-0}" = 1 ]; then
  AUTO=1
fi
if [ -n "${RACKLOOM_DISK:-}" ]; then
  DISK_ARG=$RACKLOOM_DISK
fi
if [ "${RACKLOOM_INSTALLER_NO_REBOOT:-0}" = 1 ]; then
  NO_REBOOT=1
fi

ANSWERS_DIR=${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}
export RACKLOOM_INSTALLER_ANSWERS_DIR=$ANSWERS_DIR
mkdir -p "$ANSWERS_DIR"
chmod 0700 "$ANSWERS_DIR"

TARGET_ROOT=${RACKLOOM_TARGET_ROOT:-/mnt/rackloom-target}
export RACKLOOM_TARGET_ROOT=$TARGET_ROOT

# Outils bootstrap depuis le média si absents du live.
ensure_tools() {
  if have mmdebstrap && have sgdisk && have mkfs.vfat && have mkfs.ext4; then
    return 0
  fi
  local debdir=""
  for d in /cdrom/rackloom/tool-debs /rackloom/tool-debs /cdrom-rackloom-tools; do
    if ls "$d"/*.deb >/dev/null 2>&1; then
      debdir=$d
      break
    fi
  done
  if [ -n "$debdir" ]; then
    log "installation outils depuis $debdir"
    dpkg -i "$debdir"/*.deb 2>/dev/null || apt-get -y -f install || true
  fi
  have sgdisk || die "sgdisk manquant"
  have mkfs.ext4 || die "mkfs.ext4 manquant"
  have mmdebstrap || have debootstrap || die "mmdebstrap/debootstrap manquant"
}
ensure_tools

if [ "$AUTO" = 1 ]; then
  mkdir -p /run
  exec > >(tee -a /run/rackloom-installer.log /dev/console) 2>&1
  log "mode automatique"
  TTY=/dev/console
else
  TTY=$(installer_tty) || die "aucune console pour l'installateur"
  exec <"$TTY" >"$TTY" 2>&1
  banner "$TTY"
fi

# --- Étape 1 : disque ---------------------------------------------------------
log "Étape 1 : sélection du disque"
if [ -n "$DISK_ARG" ]; then
  DISK_PATH=$DISK_ARG
elif [ "$AUTO" = 1 ]; then
  # Premier disque candidat
  DISK_PATH=$(list_candidate_disks | head -1 | cut -f1)
  [ -n "$DISK_PATH" ] || die "aucun disque candidat"
else
  printf '\nDisques candidats :\n' >"$TTY"
  mapfile -t DISKS < <(list_candidate_disks)
  if [ "${#DISKS[@]}" -eq 0 ]; then
    die "aucun disque candidat trouvé"
  fi
  i=1
  for line in "${DISKS[@]}"; do
    printf '  %d) %s\n' "$i" "$line" >"$TTY"
    i=$((i + 1))
  done
  DISK_PATH=$(prompt_line "$TTY" "Chemin du disque à effacer (ex. /dev/sda)" "")
  [ -n "$DISK_PATH" ] || die "disque requis"
  printf '\nATTENTION: toutes les données sur %s seront effacées.\n' "$DISK_PATH" >"$TTY"
  CONFIRM=$(prompt_line "$TTY" "Pour confirmer, retaper exactement le chemin du disque" "")
  [ "$CONFIRM" = "$DISK_PATH" ] || die "confirmation disque incorrecte"
  confirm_yes "$TTY" "Dernière confirmation : effacer $DISK_PATH et installer Rackloom" \
    || die "installation annulée"
fi

[ -b "$DISK_PATH" ] || die "périphérique absent: $DISK_PATH"
printf '%s\n' "$DISK_PATH" >"$ANSWERS_DIR/disk"
log "disque : $DISK_PATH"

# --- Étape 2 : partition + bootstrap + GRUB ----------------------------------
log "Étape 2 : partitionnement"
bash "$SELF_DIR/partition.sh" "$DISK_PATH"

log "Étape 2 : bootstrap Ubuntu 24.04"
bash "$SELF_DIR/bootstrap.sh"

log "Étape 2 : GRUB"
bash "$SELF_DIR/grub-install.sh" "$DISK_PATH"

# Message immédiat sur la console live
{
  printf '\n========================================\n'
  printf '  Installation Rackloom terminée\n'
  printf '  Redémarrage… retirez le média ISO.\n'
  printf '========================================\n\n'
} | tee /dev/console 2>/dev/null || true

sync
umount -R "$TARGET_ROOT" 2>/dev/null || true

if [ "$NO_REBOOT" = 1 ]; then
  log "Étape 1–2 OK (--no-reboot)"
  exit 0
fi

log "reboot"
systemctl reboot || reboot -f
