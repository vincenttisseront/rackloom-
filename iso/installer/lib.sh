#!/usr/bin/env bash
# Helpers — installateur TUI Rackloom (partition + bootstrap, pas Subiquity).
# shellcheck shell=bash

BACKTITLE="Rackloom appliance"
USERNAME_FIXED="rackloom"

die() {
  printf 'rackloom-installer: ERREUR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf 'rackloom-installer: %s\n' "$*" >&2
}

have() { command -v "$1" >/dev/null 2>&1; }

installer_tty() {
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf '%s\n' /dev/tty
    return 0
  fi
  if [ -r /dev/console ] && [ -w /dev/console ]; then
    printf '%s\n' /dev/console
    return 0
  fi
  return 1
}

banner() {
  local tty=$1
  {
    printf '\n'
    printf '========================================\n'
    printf '   Rackloom appliance installer\n'
    printf '========================================\n'
    printf ' Installateur Rackloom (pas Subiquity).\n'
    printf ' Étape 1 : disque\n'
    printf ' Étape 2 : partition + Ubuntu 24.04 + GRUB\n'
    printf '========================================\n\n'
  } >"$tty"
}

prompt_line() {
  local tty=$1 msg=$2 default=${3:-} reply
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$msg" "$default" >"$tty"
  else
    printf '%s: ' "$msg" >"$tty"
  fi
  # shellcheck disable=SC2162
  IFS= read -r reply <"$tty" || true
  if [ -z "$reply" ]; then
    reply=$default
  fi
  printf '%s\n' "$reply"
}

confirm_yes() {
  local tty=$1 msg=$2 reply
  reply=$(prompt_line "$tty" "$msg (oui/non)" "non")
  case "$reply" in
    oui|Oui|OUI|o|O|yes|Yes|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

# Liste « chemin<TAB>taille modèle » en excluant le média d'installation si possible.
list_candidate_disks() {
  local cdrom_src=""
  if [ -e /dev/disk/by-label ]; then
    :
  fi
  # Exclure le périphérique qui porte /cdrom si monté.
  if mountpoint -q /cdrom 2>/dev/null; then
    cdrom_src=$(findmnt -n -o SOURCE /cdrom 2>/dev/null || true)
    cdrom_src=${cdrom_src%%[0-9]*}
  fi
  lsblk -dn -o NAME,TYPE,SIZE,MODEL 2>/dev/null | while read -r name typ size model; do
    [ "$typ" = disk ] || continue
    path="/dev/$name"
    if [ -n "$cdrom_src" ] && [ "$path" = "$cdrom_src" ]; then
      continue
    fi
    # Exclure sr* / loop
    case "$name" in
      sr*|loop*|fd*) continue ;;
    esac
    printf '%s\t%s %s\n' "$path" "$size" "${model:-}"
  done
}
