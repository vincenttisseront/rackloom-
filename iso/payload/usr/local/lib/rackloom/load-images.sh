#!/usr/bin/env bash
# Charge les images Docker embarquées dans l'ISO (tar sous /opt/rackloom/images/).
set -euo pipefail

IMG_DIR=/opt/rackloom/images
MANIFEST="$IMG_DIR/manifest.sha256"

[ -d "$IMG_DIR" ] || { echo "load-images: répertoire absent ($IMG_DIR)" >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
  echo "load-images: docker absent" >&2
  exit 1
fi

# Attendre le démon si le socket n'est pas encore prêt.
for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info >/dev/null 2>&1 || { echo "load-images: démon Docker injoignable" >&2; exit 1; }

shopt -s nullglob
tars=("$IMG_DIR"/*.tar "$IMG_DIR"/*.tar.gz)
if [ ${#tars[@]} -eq 0 ]; then
  if [ -f "$IMG_DIR/README.txt" ]; then
    echo "load-images: ISO construite sans images ; importer un bundle F17 avant démarrage"
    exit 0
  fi
  echo "load-images: aucun archive d'image (construction sans embarquement ?)" >&2
  exit 1
fi

if [ -f "$MANIFEST" ]; then
  (cd "$IMG_DIR" && sha256sum -c manifest.sha256)
fi

for archive in "${tars[@]}"; do
  echo "load-images: chargement $(basename "$archive")"
  docker load -i "$archive"
done

echo "load-images: terminé"
