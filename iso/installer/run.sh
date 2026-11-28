#!/usr/bin/env bash
# Point d'entrée early-commands : UI console Rackloom, puis réécriture
# de /autoinstall.yaml en mode 100 % non interactif (aucune fenêtre Subiquity).
#
# L'opérateur ne voit que ce script (pas language / keyboard / mirror / snaps…).
# Sous le capot : Ubuntu 24.04 live-server + Subiquity/curtin appliquent la graine.
set -euo pipefail

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SELF_DIR/lib.sh"

ANSWERS_DIR=${RACKLOOM_INSTALLER_ANSWERS_DIR:-/run/rackloom-installer}
AUTOINSTALL_OUT=${RACKLOOM_AUTOINSTALL_OUT:-/autoinstall.yaml}
TEMPLATE=${RACKLOOM_INSTALLER_TEMPLATE:-$SELF_DIR/autoinstall.template.yaml}

mkdir -p "$ANSWERS_DIR"
chmod 0700 "$ANSWERS_DIR"

TTY=$(installer_tty) || die "aucune console (/dev/tty ou /dev/console) pour l'installateur"
exec <"$TTY" >"$TTY" 2>&1

banner "$TTY"

# --- 1. Réseau ----------------------------------------------------------------
log "configuration réseau"
printf 'Mode réseau :\n' >"$TTY"
printf '  1) DHCP (recommandé)\n' >"$TTY"
printf '  2) Adresse IPv4 statique\n' >"$TTY"
NET_CHOICE=$(prompt_line "$TTY" "Choix" "1")

NETWORK_MODE=dhcp
IFACE=""
ADDRESS=""
GATEWAY=""
NAMESERVERS=""

case "$NET_CHOICE" in
  2|statique|static|Static)
    NETWORK_MODE=static
    printf '\nInterfaces détectées :\n' >"$TTY"
    ip -br link show 2>/dev/null | awk '$1 !~ /^(lo|docker|br-|veth)/ {print "  " $1 "  " $3}' >"$TTY" || true
    IFACE=$(prompt_line "$TTY" "Interface (ex. ens160)" "")
    [ -n "$IFACE" ] || die "interface réseau requise"
    ADDRESS=$(prompt_line "$TTY" "Adresse CIDR (ex. 192.0.2.10/24)" "")
    [ -n "$ADDRESS" ] || die "adresse IPv4 requise"
    GATEWAY=$(prompt_line "$TTY" "Passerelle IPv4" "")
    [ -n "$GATEWAY" ] || die "passerelle requise"
    NAMESERVERS=$(prompt_line "$TTY" "DNS (espaces si plusieurs)" "1.1.1.1")
    ;;
  *)
    NETWORK_MODE=dhcp
    printf 'DHCP sur les interfaces ethernet (en* / eth*).\n' >"$TTY"
    ;;
esac

# --- 2. Hostname / nom --------------------------------------------------------
log "hostname"
HOSTNAME=$(prompt_line "$TTY" "Nom / hostname de l'appliance (FQDN possible)" "rackloom")
# Validation basique RFC 1123 (labels).
if ! printf '%s' "$HOSTNAME" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'; then
  die "hostname invalide: $HOSTNAME"
fi
if [ "${#HOSTNAME}" -gt 253 ]; then
  die "hostname trop long"
fi

# --- 3. Mot de passe compte initial (username figé) ---------------------------
log "compte initial ($USERNAME_FIXED)"
printf 'Compte console : %s (non modifiable)\n' "$USERNAME_FIXED" >"$TTY"
PASS1=$(prompt_secret "$TTY" "Mot de passe initial")
[ -n "$PASS1" ] || die "mot de passe requis"
PASS2=$(prompt_secret "$TTY" "Confirmer le mot de passe")
[ "$PASS1" = "$PASS2" ] || die "les mots de passe ne correspondent pas"
[ "${#PASS1}" -ge 8 ] || die "mot de passe trop court (minimum 8 caractères)"
PASSWORD_HASH=$(hash_password "$PASS1")
PASS1=""
PASS2=""
unset PASS1 PASS2

# --- 4. Disque + confirmation explicite d'effacement --------------------------
log "sélection du disque"
printf '\nDisques candidats (le média d'\''installation est exclu si détecté) :\n' >"$TTY"
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
[ -b "$DISK_PATH" ] || die "périphérique absent ou non bloquant: $DISK_PATH"

found=0
for line in "${DISKS[@]}"; do
  case "$line" in
    "$DISK_PATH"| "$DISK_PATH"$'\t'*) found=1 ;;
  esac
  # Comparer le premier champ.
  d=${line%%$'\t'*}
  if [ "$d" = "$DISK_PATH" ]; then found=1; fi
done
[ "$found" = 1 ] || die "disque non listé comme candidat: $DISK_PATH"

printf '\nATTENTION: toutes les données sur %s seront effacées.\n' "$DISK_PATH" >"$TTY"
CONFIRM=$(prompt_line "$TTY" "Pour confirmer, retaper exactement le chemin du disque" "")
[ "$CONFIRM" = "$DISK_PATH" ] || die "confirmation disque incorrecte — installation annulée"
confirm_yes "$TTY" "Dernière confirmation : effacer $DISK_PATH et installer Rackloom" \
  || die "installation annulée par l'opérateur"

# --- Synthèse -----------------------------------------------------------------
printf '\n--- Récapitulatif ---\n' >"$TTY"
printf ' Réseau   : %s\n' "$NETWORK_MODE" >"$TTY"
if [ "$NETWORK_MODE" = static ]; then
  printf '   iface  : %s\n' "$IFACE" >"$TTY"
  printf '   addr   : %s\n' "$ADDRESS" >"$TTY"
  printf '   gw     : %s\n' "$GATEWAY" >"$TTY"
  printf '   dns    : %s\n' "$NAMESERVERS" >"$TTY"
fi
printf ' Hostname : %s\n' "$HOSTNAME" >"$TTY"
printf ' Compte   : %s (mot de passe défini)\n' "$USERNAME_FIXED" >"$TTY"
printf ' Disque   : %s (confirmé)\n' "$DISK_PATH" >"$TTY"
printf '--------------------\n\n' >"$TTY"
confirm_yes "$TTY" "Lancer l'installation automatique" || die "installation annulée"

# --- Écriture des réponses (hash seulement, jamais le clair) ------------------
ANSWERS="$ANSWERS_DIR/answers.env"
umask 077
{
  printf 'HOSTNAME=%q\n' "$HOSTNAME"
  printf 'USERNAME=%q\n' "$USERNAME_FIXED"
  printf 'REALNAME=%q\n' "$REALNAME_FIXED"
  printf 'PASSWORD_HASH=%q\n' "$PASSWORD_HASH"
  printf 'DISK_PATH=%q\n' "$DISK_PATH"
  printf 'NETWORK_MODE=%q\n' "$NETWORK_MODE"
  printf 'IFACE=%q\n' "$IFACE"
  printf 'ADDRESS=%q\n' "$ADDRESS"
  printf 'GATEWAY=%q\n' "$GATEWAY"
  printf 'NAMESERVERS=%q\n' "$NAMESERVERS"
} >"$ANSWERS"
chmod 0600 "$ANSWERS"

# --- Rendu autoinstall non interactif ----------------------------------------
# shellcheck source=render-autoinstall.sh
source "$SELF_DIR/render-autoinstall.sh"
render_autoinstall "$ANSWERS" "$TEMPLATE" "$AUTOINSTALL_OUT"

# Marqueur pour firstboot : hostname déjà choisi à l'install.
mkdir -p /cdrom/rackloom 2>/dev/null || true
printf '%s\n' "$HOSTNAME" >"$ANSWERS_DIR/hostname" || true

log "autoinstall non interactif écrit dans $AUTOINSTALL_OUT"
log "Subiquity poursuivra sans écran Ubuntu (language, clavier, miroir, snaps…)"
exit 0
