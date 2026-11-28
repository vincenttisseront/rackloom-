#!/usr/bin/env bash
# Smoke test hors ISO : applique le branding sur une copie de fixture GRUB Ubuntu 24.04.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/boot/grub" "$WORK/isolinux"
cp "$ROOT/iso/branding/fixtures/ubuntu-24.04-grub.cfg" "$WORK/boot/grub/grub.cfg"
cp "$ROOT/iso/branding/fixtures/ubuntu-24.04-grub.cfg" "$WORK/boot/grub/loopback.cfg"
printf 'label install\n  menu label ^Try or Install Ubuntu Server\n' >"$WORK/isolinux/txt.cfg"
# Splash isolinux factice pour vérifier le remplacement.
printf 'PNG' >"$WORK/isolinux/splash.png"

bash "$ROOT/iso/branding/apply-grub-branding.sh" "$WORK" "$ROOT/iso/branding"

grep -q 'Install Rackloom appliance' "$WORK/boot/grub/grub.cfg"
grep -q 'Install Rackloom appliance (safe graphics)' "$WORK/boot/grub/grub.cfg"
! grep -q 'Try or Install Ubuntu Server' "$WORK/boot/grub/grub.cfg"
grep -q 'rackloom-grub-branding' "$WORK/boot/grub/grub.cfg"
grep -q 'background_image' "$WORK/boot/grub/grub.cfg"
test -f "$WORK/boot/grub/theme/splash.png"
test -f "$WORK/boot/grub/theme/theme.txt"
test -f "$WORK/boot/grub/splash.png"
grep -q 'Install Rackloom appliance' "$WORK/isolinux/txt.cfg"
# Idempotence
bash "$ROOT/iso/branding/apply-grub-branding.sh" "$WORK" "$ROOT/iso/branding"
count=$(grep -c 'rackloom-grub-branding' "$WORK/boot/grub/grub.cfg" || true)
# Un seul marqueur d'ouverture (le bloc contient aussi "end rackloom-grub-branding")
open_count=$(grep -c '^# rackloom-grub-branding$' "$WORK/boot/grub/grub.cfg")
[ "$open_count" = 1 ]

printf 'OK branding GRUB fixture\n'
