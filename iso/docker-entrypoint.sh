#!/usr/bin/env bash
# Point d'entree de rackloom/iso-builder:appliance.
# Filet : normalise CRLF -> LF sur iso/ monte depuis Windows, puis lance build.sh.
# Ce script est COPIE dans l'image (LF fige) ; il ne depend pas du checkout hote.
set -euo pipefail

if [ -f /src/iso/build.sh ]; then
  ROOT=/src
else
  ROOT=$(cd "$(dirname "$0")/.." && pwd)
fi

normalize_crlf() {
  local f=$1
  [ -f "$f" ] || return 0
  sed -i 's/\r$//' "$f"
}

printf '== %s\n' "normalisation CRLF sous iso/ (filet Windows)"
while IFS= read -r -d '' f; do
  normalize_crlf "$f"
done < <(
  find "$ROOT/iso" -type f \( \
    -name '*.sh' -o -name '*.env' -o -name '*.ps1' \
    -o -name '*.yaml' -o -name '*.yml' -o -name '*.template' \
    -o -name 'user-data' -o -name 'user-data.test' -o -name 'meta-data' \
    -o -name 'Dockerfile*' -o -name 'VERSION' -o -name 'README.md' \
    -o -name '*.service' -o -name 'rackloom-firstboot' \
  \) -print0 2>/dev/null
)

exec bash "$ROOT/iso/build.sh" "$@"
