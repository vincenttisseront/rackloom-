#!/usr/bin/env bash
# Applique le branding Rackloom au menu GRUB / isolinux d'une ISO Ubuntu live extraite.
# Usage : apply-grub-branding.sh <extract-root> <branding-dir>
set -euo pipefail

EXTRACT=${1:?extract root requis}
BRAND=${2:?branding dir requis}

need_file() { [ -f "$1" ] || { echo "fichier branding absent: $1" >&2; exit 1; }; }
need_file "$BRAND/splash.png"
need_file "$BRAND/logo.png"
need_file "$BRAND/theme.txt"

THEME_DST="$EXTRACT/boot/grub/theme"
mkdir -p "$THEME_DST"
cp -a "$BRAND/splash.png" "$BRAND/logo.png" "$BRAND/theme.txt" "$THEME_DST/"
# Alias attendu par certains chargeurs Ubuntu.
cp -a "$BRAND/splash.png" "$EXTRACT/boot/grub/splash.png"

SNIPPET=$(mktemp)
trap 'rm -f "$SNIPPET"' EXIT
cat >"$SNIPPET" <<'EOF'
# rackloom-grub-branding
insmod all_video
insmod gfxterm
insmod png
insmod jpeg
if [ -f ${prefix}/fonts/unicode.pf2 ]; then
  loadfont ${prefix}/fonts/unicode.pf2
fi
set gfxmode=auto
set gfxpayload=keep
terminal_output gfxterm
set color_normal=light-gray/black
set color_highlight=white/blue
set menu_color_normal=light-gray/black
set menu_color_highlight=white/blue
if [ -f ${prefix}/theme/theme.txt ]; then
  set theme=${prefix}/theme/theme.txt
  export theme
elif [ -f /boot/grub/theme/theme.txt ]; then
  set theme=/boot/grub/theme/theme.txt
  export theme
fi
if [ -f ${prefix}/splash.png ]; then
  background_image -m stretch ${prefix}/splash.png
elif [ -f /boot/grub/splash.png ]; then
  background_image -m stretch /boot/grub/splash.png
fi
# end rackloom-grub-branding
EOF

# --- Libellés menu (Ubuntu Server live 24.04) --------------------------------
rename_labels() {
  local f=$1
  [ -f "$f" ] || return 0
  if grep -q 'Install Rackloom appliance' "$f" 2>/dev/null; then
    return 0
  fi
  sed -i \
    -e 's/Try or Install Ubuntu Server/Install Rackloom appliance/g' \
    -e 's/Try or install Ubuntu/Install Rackloom appliance/g' \
    -e 's/Ubuntu Server (safe graphics)/Install Rackloom appliance (safe graphics)/g' \
    -e 's/Ubuntu Server with the HWE kernel/Install Rackloom appliance (HWE kernel)/g' \
    -e 's/Install Ubuntu Server/Install Rackloom appliance/g' \
    "$f"
}

# --- Injection thème / couleurs / image de fond ------------------------------
inject_theme() {
  local f=$1
  [ -f "$f" ] || return 0
  if grep -q 'rackloom-grub-branding' "$f" 2>/dev/null; then
    return 0
  fi

  # Neutralise les couleurs Ubuntu qui suivraient sinon notre bloc.
  sed -i \
    -e 's/^set menu_color_normal=.*/set menu_color_normal=light-gray\/black/' \
    -e 's/^set menu_color_highlight=.*/set menu_color_highlight=white\/blue/' \
    -e 's/^set color_normal=.*/set color_normal=light-gray\/black/' \
    -e 's/^set color_highlight=.*/set color_highlight=white\/blue/' \
    "$f"

  local tmp
  tmp=$(mktemp)
  # Insère juste avant la première entrée de menu (après le préambule Ubuntu).
  if grep -qE '^[[:space:]]*menuentry ' "$f"; then
    awk '
      BEGIN { done=0 }
      {
        if (!done && $0 ~ /^[[:space:]]*menuentry /) {
          while ((getline line < snip) > 0) print line
          close(snip)
          done=1
        }
        print
      }
      END {
        if (!done) {
          while ((getline line < snip) > 0) print line
          close(snip)
        }
      }
    ' snip="$SNIPPET" "$f" >"$tmp"
  else
    cat "$f" "$SNIPPET" >"$tmp"
  fi
  mv "$tmp" "$f"
}

for cfg in \
  "$EXTRACT/boot/grub/grub.cfg" \
  "$EXTRACT/boot/grub/loopback.cfg" \
  "$EXTRACT/EFI/ubuntu/grub.cfg" \
  "$EXTRACT/EFI/BOOT/grub.cfg"
do
  rename_labels "$cfg"
  inject_theme "$cfg"
done

# Isolinux / syslinux (BIOS legacy, si présent sur l'ISO amont).
for cfg in \
  "$EXTRACT/isolinux/txt.cfg" \
  "$EXTRACT/isolinux/menu.cfg" \
  "$EXTRACT/syslinux/txt.cfg"
do
  [ -f "$cfg" ] || continue
  sed -i \
    -e 's/Try or Install Ubuntu Server/Install Rackloom appliance/g' \
    -e 's/Install Ubuntu Server/Install Rackloom appliance/g' \
    -e 's/Ubuntu Server/Rackloom appliance/g' \
    "$cfg"
done

if [ -f "$EXTRACT/isolinux/splash.png" ]; then
  cp -a "$BRAND/splash.png" "$EXTRACT/isolinux/splash.png"
fi
if [ -f "$EXTRACT/syslinux/splash.png" ]; then
  cp -a "$BRAND/splash.png" "$EXTRACT/syslinux/splash.png"
fi

printf 'branding Rackloom applique sous %s\n' "$EXTRACT/boot/grub/theme"
