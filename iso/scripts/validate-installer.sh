#!/usr/bin/env bash
# Valide l'architecture installateur Rackloom (TUI réel, pas Subiquity UX).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FAIL=0
fail() { printf 'FAIL: %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
ok() { printf 'OK: %s\n' "$*"; }

echo "== validate installateur Rackloom (RAC-62) =="

INST="$ROOT/iso/installer"
[ -d "$INST" ] || fail "iso/installer/ absent"
for f in run.sh lib.sh partition.sh bootstrap.sh grub-install.sh; do
  [ -f "$INST/$f" ] || fail "manquant: installer/$f"
done
ok "iso/installer/ complet"

if [ -e "$INST/render-autoinstall.sh" ] || [ -e "$INST/autoinstall.template.yaml" ]; then
  fail "artefacts autoinstall #52 encore présents"
else
  ok "pas de rendu autoinstall"
fi
if grep -Eq 'AUTOINSTALL_OUT|/autoinstall\.yaml|render_autoinstall' "$INST/run.sh"; then
  fail "run.sh encore lié à autoinstall"
else
  ok "run.sh indépendant de Subiquity/autoinstall"
fi
if ! grep -Eq 'mmdebstrap|debootstrap' "$INST/bootstrap.sh"; then
  fail "bootstrap sans mmdebstrap/debootstrap"
else
  ok "bootstrap mmdebstrap/debootstrap"
fi

# shellcheck source=../versions.env
source "$ROOT/iso/versions.env"
[ "${UBUNTU_RELEASE:-}" = "24.04" ] || fail "UBUNTU_RELEASE != 24.04"
ok "versions.env : $UBUNTU_ISO_NAME"

if ! grep -Eq 'subiquity|squashfs|rackloom-installer' "$ROOT/iso/build.sh"; then
  fail "build.sh ne masque pas Subiquity / n'embarque pas l'installateur"
else
  ok "build.sh chemin installateur réel"
fi

[ -f "$INST/systemd/rackloom-installer.service" ] || fail "unité systemd live absente"
ok "systemd rackloom-installer.service"

[ -f "$ROOT/iso/payload/usr/local/lib/rackloom/firstboot.sh" ] || fail "firstboot absent"
ok "firstboot payload"

if [ "$FAIL" -gt 0 ]; then
  echo "validate : $FAIL échec(s)" >&2
  exit 1
fi
echo "validate : OK"
exit 0
