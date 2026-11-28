#!/usr/bin/env bash
# Helpers partagés — installateur console Rackloom (Lot 3 / RAC-61).
# shellcheck shell=bash

BACKTITLE="Rackloom appliance"
USERNAME_FIXED="rackloom"
REALNAME_FIXED="Rackloom"

die() {
  printf 'rackloom-installer: ERREUR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf 'rackloom-installer: %s\n' "$*" >&2
}

have() { command -v "$1" >/dev/null 2>&1; }

# Console TTY pour les invites (early-commands Subiquity).
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
    printf ' Choix opérateur uniquement :\n'
    printf '   1) réseau\n'
    printf '   2) nom / hostname\n'
    printf '   3) mot de passe compte initial\n'
    printf '   4) confirmation explicite du disque\n'
    printf ' Le reste est automatisé (autoinstall).\n'
    printf '========================================\n\n'
  } >"$tty"
}

prompt_line() {
  # prompt_line <tty> <message> [default] -> écrit la réponse sur stdout
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

prompt_secret() {
  local tty=$1 msg=$2 reply
  printf '%s: ' "$msg" >"$tty"
  stty -echo <"$tty" 2>/dev/null || true
  # shellcheck disable=SC2162
  IFS= read -r reply <"$tty" || true
  stty echo <"$tty" 2>/dev/null || true
  printf '\n' >"$tty"
  printf '%s\n' "$reply"
}

confirm_yes() {
  local tty=$1 msg=$2 reply
  reply=$(prompt_line "$tty" "$msg (oui/non)" "non")
  case "$reply" in
    oui|Oui|OUI|o|O|yes|Yes|YES|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

hash_password() {
  local plain=$1
  if have openssl; then
    openssl passwd -6 -- "$plain"
    return 0
  fi
  if have mkpasswd; then
    mkpasswd -m sha-512 -- "$plain"
    return 0
  fi
  die "openssl ou mkpasswd requis pour hacher le mot de passe"
}

# Disques candidats (exclut loop, rom, et le périphérique du média live si détectable).
list_candidate_disks() {
  local live_src=""
  if [ -r /cdrom ]; then
    live_src=$(findmnt -n -o SOURCE /cdrom 2>/dev/null || true)
    live_src=${live_src%%[0-9]*}
  fi
  lsblk -dn -o NAME,SIZE,TYPE,MODEL 2>/dev/null | while read -r name size typ model; do
    [ "$typ" = disk ] || continue
    case "$name" in
      loop*|sr*|ram*|zram*) continue ;;
    esac
    if [ -n "$live_src" ] && [ "/dev/$name" = "$live_src" ]; then
      continue
    fi
    printf '/dev/%s\t%s\t%s\n' "$name" "$size" "${model:-}"
  done
}

yaml_single_quote() {
  # Échappe une valeur pour YAML en simple quotes ('' pour ').
  local s=$1
  s=${s//\'/\'\'}
  printf "'%s'" "$s"
}
