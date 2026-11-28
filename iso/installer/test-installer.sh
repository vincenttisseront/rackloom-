#!/usr/bin/env bash
# Smoke hors ISO : rendu autoinstall + validations (inclus dans make test).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LIB="$ROOT/iso/installer/lib.sh"
RENDER="$ROOT/iso/installer/render-autoinstall.sh"
TEMPLATE="$ROOT/iso/installer/autoinstall.template.yaml"
RUN="$ROOT/iso/installer/run.sh"

# shellcheck source=lib.sh
source "$LIB"
# shellcheck source=render-autoinstall.sh
source "$RENDER"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'OK %s\n' "$*"; }

# --- Fichiers présents --------------------------------------------------------
[ -x "$RUN" ] || chmod +x "$RUN" "$ROOT/iso/installer/"*.sh
[ -f "$TEMPLATE" ] || fail "template manquant"
grep -q '__RACKLOOM_NETWORK_BLOCK__' "$TEMPLATE" || fail "marqueur réseau absent"
grep -q '__RACKLOOM_HOSTNAME_RAW__' "$TEMPLATE" || fail "marqueur hostname raw absent"
! grep -qi 'interactive-sections' "$TEMPLATE" || fail "template ne doit pas avoir interactive-sections"
pass "template"

# --- user-data bootstrap : early-commands, pas d'UI Subiquity -----------------
UD="$ROOT/iso/autoinstall/user-data"
grep -q 'bash /cdrom/rackloom/installer/run.sh' "$UD" || fail "early-commands absents"
! grep -q 'interactive-sections:' "$UD" || fail "user-data ne doit plus exposer interactive-sections"
grep -q 'rackloom-installer-must-select-disk' "$UD" || fail "disque stub fail-closed absent"
pass "user-data bootstrap"

# --- Rendu DHCP ---------------------------------------------------------------
ANSWERS="$WORK/answers.env"
cat >"$ANSWERS" <<'EOF'
HOSTNAME='rackloom.lab.invalid'
USERNAME='rackloom'
REALNAME='Rackloom'
PASSWORD_HASH='$6$testsalt$testhashvalueonlyforsmokeabcdefghijklmnopqrstuv'
DISK_PATH='/dev/vda'
NETWORK_MODE='dhcp'
IFACE=''
ADDRESS=''
GATEWAY=''
NAMESERVERS=''
EOF

OUT="$WORK/autoinstall.yaml"
render_autoinstall "$ANSWERS" "$TEMPLATE" "$OUT"

grep -q "hostname: 'rackloom.lab.invalid'" "$OUT" || fail "hostname non rendu"
grep -q "username: 'rackloom'" "$OUT" || fail "username non rendu"
grep -q "path: '/dev/vda'" "$OUT" || fail "disque non rendu"
grep -q 'dhcp4: true' "$OUT" || fail "réseau DHCP absent"
grep -q "rackloom.lab.invalid > /etc/rackloom/install-hostname" "$OUT" || fail "hostname raw late-command"
! grep -q '__RACKLOOM_' "$OUT" || fail "marqueur non substitué: $(grep -n '__RACKLOOM_' "$OUT" || true)"
! grep -qi 'interactive-sections' "$OUT" || fail "rendu interactif interdit"
! grep -qi 'Try or Install' "$OUT" || fail "libellé Ubuntu dans autoinstall"
pass "rendu DHCP"

# --- Rendu statique -----------------------------------------------------------
cat >"$ANSWERS" <<'EOF'
HOSTNAME='app01'
USERNAME='rackloom'
REALNAME='Rackloom'
PASSWORD_HASH='$6$x$y'
DISK_PATH='/dev/sda'
NETWORK_MODE='static'
IFACE='ens160'
ADDRESS='192.0.2.10/24'
GATEWAY='192.0.2.1'
NAMESERVERS='192.0.2.53 192.0.2.54'
EOF
OUT2="$WORK/autoinstall-static.yaml"
render_autoinstall "$ANSWERS" "$TEMPLATE" "$OUT2"
grep -q 'ens160:' "$OUT2" || fail "iface statique absente"
grep -q '192.0.2.10/24' "$OUT2" || fail "adresse absente"
grep -q '192.0.2.1' "$OUT2" || fail "passerelle absente"
grep -q '192.0.2.53' "$OUT2" || fail "DNS absent"
pass "rendu statique"

# --- yaml_single_quote --------------------------------------------------------
q=$(yaml_single_quote "a'b")
[ "$q" = "'a''b'" ] || fail "yaml_single_quote: got $q"
pass "yaml_single_quote"

# --- Embarquement attendu dans build.sh --------------------------------------
grep -q 'iso/installer' "$ROOT/iso/build.sh" || fail "build.sh n'embarque pas installer/"
grep -q 'PAYLOAD_DST/installer' "$ROOT/iso/build.sh" || fail "build.sh n'écrit pas installer sur l'ISO"
pass "build.sh embarque installer"

printf 'OK installateur console Rackloom (smoke)\n'
