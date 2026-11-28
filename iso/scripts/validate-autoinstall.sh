#!/usr/bin/env bash
# Valide que l'ISO appliance reste sur le chemin Subiquity / Curtin officiel.
# Échoue si un installateur maison ou un early-commands UI est présent.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FAIL=0

fail() { printf 'FAIL: %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
ok() { printf 'OK: %s\n' "$*"; }

echo "== validate autoinstall Subiquity (RAC-62) =="

# 1. Aucun installateur maison
if [ -e "$ROOT/iso/installer" ]; then
  fail "iso/installer/ présent — installateur maison interdit (utiliser Subiquity)"
else
  ok "pas de iso/installer/"
fi

# 2. versions.env : ISO amont épinglée
# shellcheck source=../versions.env
source "$ROOT/iso/versions.env"
[ -n "${UBUNTU_ISO_NAME:-}" ] || fail "UBUNTU_ISO_NAME vide"
[ -n "${UBUNTU_ISO_URL:-}" ] || fail "UBUNTU_ISO_URL vide"
[ -n "${UBUNTU_SHA256SUMS_URL:-}" ] || fail "UBUNTU_SHA256SUMS_URL vide"
[ "${UBUNTU_RELEASE:-}" = "24.04" ] || fail "UBUNTU_RELEASE doit être 24.04 (got ${UBUNTU_RELEASE:-})"
ok "versions.env : $UBUNTU_ISO_NAME ($UBUNTU_RELEASE)"

# 3. user-data production
UD="$ROOT/iso/autoinstall/user-data"
[ -f "$UD" ] || fail "user-data absent"
# Clés YAML only (ignore commentaires qui mentionnent early-commands).
if grep -Eiq '^[[:space:]]*early-commands[[:space:]]*:' "$UD"; then
  fail "user-data : early-commands interdit (pas d'installateur console maison)"
fi
if grep -Eiq 'rackloom/installer|/installer/run\.sh' "$UD"; then
  fail "user-data : référence à iso/installer"
fi
if ! grep -Eq 'interactive-sections:' "$UD"; then
  fail "user-data : interactive-sections requis (storage via Subiquity)"
fi
if ! grep -Eq '^[[:space:]]+- storage' "$UD"; then
  fail "user-data : section interactive storage manquante"
fi
if ! grep -Eq '^[[:space:]]+- identity' "$UD"; then
  fail "user-data : section interactive identity manquante"
fi
# Pas de clé password embarquée (Subiquity identity la demande).
if grep -Eq '^[[:space:]]+password:' "$UD"; then
  fail "user-data : password embarqué interdit (demander via Subiquity identity)"
fi
ok "user-data production : Subiquity interactif, pas d'early-commands"

# 4. user-data.test (CI / QEMU)
UDT="$ROOT/iso/autoinstall/user-data.test"
[ -f "$UDT" ] || fail "user-data.test absent"
if grep -Eiq '^[[:space:]]*early-commands[[:space:]]*:' "$UDT" \
  || grep -Eiq 'rackloom/installer' "$UDT"; then
  fail "user-data.test : early-commands / installer maison"
fi
if ! grep -Eq 'path:[[:space:]]*/dev/vda' "$UDT"; then
  fail "user-data.test : disque /dev/vda attendu pour QEMU"
fi
if ! grep -Eq 'iso-test-installed' "$UDT"; then
  fail "user-data.test : marqueur iso-test-installed manquant"
fi
ok "user-data.test : graine QEMU non interactive"

# 5. build.sh ne doit plus embarquer installer/
if grep -Eq 'iso/installer|PAYLOAD_DST/installer|rackloom/installer' "$ROOT/iso/build.sh"; then
  fail "build.sh embarque encore iso/installer"
else
  ok "build.sh : pas d'embarquement installer/"
fi

# 6. Makefile : plus de test-installer maison
if grep -Eq 'iso/installer/test-installer' "$ROOT/Makefile"; then
  fail "Makefile appelle encore test-installer.sh"
else
  ok "Makefile : plus de test-installer maison"
fi

# 7. firstboot idempotent présent
FB="$ROOT/iso/payload/usr/local/lib/rackloom/firstboot.sh"
[ -f "$FB" ] || fail "firstboot.sh absent"
if ! grep -Eq 'firstboot\.done|MARKER' "$FB"; then
  fail "firstboot.sh : marqueur d'idempotence absent"
fi
ok "firstboot.sh idempotent"

if [ "$FAIL" -gt 0 ]; then
  echo "validate : $FAIL échec(s)" >&2
  exit 1
fi
echo "validate : OK"
exit 0
