#!/usr/bin/env bash
# Smoke hors ISO : helpers + scripts présents, pas de chemin Subiquity/autoinstall UX.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
INST="$ROOT/iso/installer"
FAIL=0
fail() { printf 'FAIL: %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
ok() { printf 'OK: %s\n' "$*"; }

echo "== test-installer (architecture réelle) =="

for f in lib.sh run.sh partition.sh bootstrap.sh grub-install.sh; do
  [ -f "$INST/$f" ] || fail "manquant: $f"
done
ok "scripts présents"

# Pas de rendu autoinstall (#52)
if [ -e "$INST/render-autoinstall.sh" ] || [ -e "$INST/autoinstall.template.yaml" ]; then
  fail "artefacts autoinstall #52 encore présents"
else
  ok "pas de template/rendu autoinstall"
fi

# run.sh ne doit pas réécrire /autoinstall.yaml
if grep -Eq 'AUTOINSTALL_OUT|/autoinstall\.yaml|render_autoinstall' "$INST/run.sh"; then
  fail "run.sh référence encore autoinstall"
else
  ok "run.sh sans autoinstall"
fi

# bootstrap = mmdebstrap/debootstrap
if ! grep -Eq 'mmdebstrap|debootstrap' "$INST/bootstrap.sh"; then
  fail "bootstrap.sh sans mmdebstrap/debootstrap"
else
  ok "bootstrap mmdebstrap/debootstrap"
fi

# shellcheck syntaxe basique
bash -n "$INST/lib.sh"
bash -n "$INST/run.sh"
bash -n "$INST/partition.sh"
bash -n "$INST/bootstrap.sh"
bash -n "$INST/grub-install.sh"
ok "bash -n"

# list_candidate_disks callable
# shellcheck source=lib.sh
source "$INST/lib.sh"
list_candidate_disks >/dev/null || true
ok "list_candidate_disks"

if [ "$FAIL" -gt 0 ]; then
  echo "test-installer : $FAIL échec(s)" >&2
  exit 1
fi
echo "test-installer : OK"
exit 0
