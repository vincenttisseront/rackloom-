#!/usr/bin/env bash
# Construit l'ISO appliance Rackloom à partir de l'ISO Ubuntu Server 24.04 LTS
# (vérifiée SHA-256 + signature GPG) — ADR-0004.
#
# Usage :
#   ./iso/build.sh              # ISO de production (stockage interactif)
#   ./iso/build.sh --test-seed  # graine autoinstall non interactive (CI / test-iso)
#   RACKLOOM_ISO_SKIP_IMAGES=1 ./iso/build.sh
#
# Sortie : dist/rackloom-appliance-<version>-amd64.iso (+ .sha256 + .manifest.txt)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=versions.env
source "$ROOT/iso/versions.env"

TEST_SEED=0
EMBED_IMAGES="${RACKLOOM_ISO_EMBED_IMAGES:-1}"
SKIP_IMAGES="${RACKLOOM_ISO_SKIP_IMAGES:-0}"
[ "$SKIP_IMAGES" = 1 ] && EMBED_IMAGES=0

for arg in "$@"; do
  case "$arg" in
    --test-seed) TEST_SEED=1 ;;
    --skip-images) EMBED_IMAGES=0 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "option inconnue: $arg" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "outil requis absent: $1" >&2; exit 1; }; }
need xorriso
need curl
need gpg
need sha256sum
need docker

CACHE="${RACKLOOM_ISO_CACHE:-${TMPDIR:-/tmp}/rackloom-iso-cache}"
WORK="${RACKLOOM_ISO_WORK:-${TMPDIR:-/tmp}/rackloom-iso-work}"
OUT_DIR="${RACKLOOM_ISO_OUT:-$ROOT/dist}"
mkdir -p "$CACHE" "$WORK" "$OUT_DIR"

STAMP=$(date -u +%Y%m%d)
GIT_SHA=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
VERSION_LABEL="lot3-${STAMP}-${GIT_SHA}"
ISO_OUT="$OUT_DIR/rackloom-appliance-${UBUNTU_ISO_VERSION}-${STAMP}-amd64.iso"
[ "$TEST_SEED" = 1 ] && ISO_OUT="$OUT_DIR/rackloom-appliance-${UBUNTU_ISO_VERSION}-${STAMP}-test-amd64.iso"

say() { printf '\n== %s\n' "$*"; }

# --- 1. ISO amont ------------------------------------------------------------
say "ISO Ubuntu $UBUNTU_ISO_VERSION (cache $CACHE)"
ISO_SRC="$CACHE/$UBUNTU_ISO_NAME"
SUMS="$CACHE/SHA256SUMS"
SUMS_GPG="$CACHE/SHA256SUMS.gpg"

curl -fsSL -o "$SUMS" "$UBUNTU_SHA256SUMS_URL"
curl -fsSL -o "$SUMS_GPG" "$UBUNTU_SHA256SUMS_GPG_URL"

if ! gpg --list-keys "$UBUNTU_CD_SIGNING_KEY" >/dev/null 2>&1; then
  curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${UBUNTU_CD_SIGNING_KEY}" \
    | gpg --import
fi
gpg --verify "$SUMS_GPG" "$SUMS"

EXPECTED=$(awk -v n="$UBUNTU_ISO_NAME" '$2 == "*"n || $2 == n {print $1; exit}' "$SUMS")
[ -n "$EXPECTED" ] || { echo "empreinte absente de SHA256SUMS pour $UBUNTU_ISO_NAME" >&2; exit 1; }

if [ ! -f "$ISO_SRC" ]; then
  say "téléchargement $UBUNTU_ISO_URL"
  curl -fL --retry 3 -o "$ISO_SRC.partial" "$UBUNTU_ISO_URL"
  mv "$ISO_SRC.partial" "$ISO_SRC"
fi
echo "$EXPECTED  $ISO_SRC" | sha256sum -c -

# --- 2. Images Docker (optionnel) --------------------------------------------
IMG_STAGE="$WORK/images"
rm -rf "$IMG_STAGE"
mkdir -p "$IMG_STAGE"

if [ "$EMBED_IMAGES" = 1 ]; then
  say "construction / sauvegarde des images :$RACKLOOM_IMAGE_TAG"
  export DOCKER_BUILDKIT=1
  NET_ARGS=(--network=host)
  # Réutilise les images locales si déjà présentes (make images / build précédent).
  for pair in \
    "rackloom/backend:$RACKLOOM_IMAGE_TAG|$ROOT/backend/Dockerfile" \
    "rackloom/launcher:$RACKLOOM_IMAGE_TAG|$ROOT/launcher/Dockerfile" \
    "rackloom/runner-ansible:$RACKLOOM_IMAGE_TAG|$ROOT/runners/ansible/Dockerfile" \
    "rackloom/web:$RACKLOOM_IMAGE_TAG|$ROOT/compose/caddy/Dockerfile" \
    "rackloom/runner-terraform:$RACKLOOM_IMAGE_TAG|$ROOT/runners/terraform/Dockerfile"
  do
    ref=${pair%%|*}
    df=${pair#*|}
    if ! docker image inspect "$ref" >/dev/null 2>&1; then
      docker build "${NET_ARGS[@]}" -f "$df" -t "$ref" "$ROOT"
    else
      echo "-- image déjà présente : $ref"
    fi
  done
  # D3 (ouverte) : runner Terraform = binaire HashiCorp BSL 1.1 ; avis juridique avant publication.
  docker pull postgres:16.11-bookworm

  : >"$IMG_STAGE/manifest.sha256"
  : >"$IMG_STAGE/image-ids.txt"
  save_one() {
    local ref=$1 name=$2
    local dest="$IMG_STAGE/$name.tar"
    echo "-- docker save $ref → $name.tar"
    docker save -o "$dest" "$ref"
    (cd "$IMG_STAGE" && sha256sum "$name.tar" >>manifest.sha256)
    docker image inspect --format '{{.Id}} {{index .RepoTags 0}}' "$ref" >>"$IMG_STAGE/image-ids.txt"
  }
  save_one "rackloom/backend:$RACKLOOM_IMAGE_TAG" backend
  save_one "rackloom/launcher:$RACKLOOM_IMAGE_TAG" launcher
  save_one "rackloom/web:$RACKLOOM_IMAGE_TAG" web
  save_one "rackloom/runner-ansible:$RACKLOOM_IMAGE_TAG" runner-ansible
  save_one "rackloom/runner-terraform:$RACKLOOM_IMAGE_TAG" runner-terraform
  save_one "postgres:16.11-bookworm" postgres
else
  say "images Docker non embarquées (RACKLOOM_ISO_SKIP_IMAGES=1)"
  echo "images non embarquées ; fournir un bundle F17 ou reconstruire avec embarquement" >"$IMG_STAGE/README.txt"
fi

# --- 3. Paquets Docker hors ligne --------------------------------------------
DEB_STAGE="$WORK/debs"
rm -rf "$DEB_STAGE"
mkdir -p "$DEB_STAGE"
say "paquets Docker hors ligne"
if [ -d "${RACKLOOM_ISO_DEB_DIR:-}" ]; then
  cp -a "${RACKLOOM_ISO_DEB_DIR}/." "$DEB_STAGE/"
else
  (
    cd "$DEB_STAGE"
    apt-get download docker.io docker-compose-v2 containerd runc iptables \
      libip4tc2 libip6tc2 libxtables12 libnfnetlink0 libnetfilter-conntrack3 \
      libnftnl11 libnftables1 libmnl0
  )
fi
(cd "$DEB_STAGE" && sha256sum ./*.deb >manifest.sha256)

# --- 4. Extraction et remaster -----------------------------------------------
say "extraction de l'ISO amont"
EXTRACT="$WORK/extract"
rm -rf "$EXTRACT"
mkdir -p "$EXTRACT"
# Les fichiers issus de l'ISO sont souvent en lecture seule.
xorriso -osirrox on -indev "$ISO_SRC" -extract / "$EXTRACT"
chmod -R u+w "$EXTRACT"

say "injection autoinstall + payload Rackloom"
NOCLOUD="$EXTRACT/nocloud"
mkdir -p "$NOCLOUD"
if [ "$TEST_SEED" = 1 ]; then
  cp "$ROOT/iso/autoinstall/user-data.test" "$NOCLOUD/user-data"
else
  cp "$ROOT/iso/autoinstall/user-data" "$NOCLOUD/user-data"
fi
cp "$ROOT/iso/autoinstall/meta-data" "$NOCLOUD/meta-data"

PAYLOAD_DST="$EXTRACT/rackloom"
rm -rf "$PAYLOAD_DST"
mkdir -p "$PAYLOAD_DST/payload" "$PAYLOAD_DST/debs"
cp -a "$ROOT/iso/payload/." "$PAYLOAD_DST/payload/"
printf '%s\n' "$VERSION_LABEL" >"$PAYLOAD_DST/payload/etc/rackloom/VERSION"
cp -a "$DEB_STAGE/." "$PAYLOAD_DST/debs/"
mkdir -p "$PAYLOAD_DST/payload/opt/rackloom/images"
if [ "$EMBED_IMAGES" = 1 ]; then
  cp -a "$IMG_STAGE/." "$PAYLOAD_DST/payload/opt/rackloom/images/"
else
  cp -a "$IMG_STAGE/README.txt" "$PAYLOAD_DST/payload/opt/rackloom/images/" 2>/dev/null || true
fi

cat >"$PAYLOAD_DST/install-debs.sh" <<'EOS'
#!/bin/bash
set -euo pipefail
DEBDIR=/cdrom/rackloom/debs
cp -a "$DEBDIR" /target/tmp/rackloom-debs
# Ordre approximatif : libs → runc/containerd → docker → compose
chroot /target bash -c '
  set -e
  cd /tmp/rackloom-debs
  dpkg -i ./libmnl0_*.deb ./libnfnetlink0_*.deb ./libnftnl11_*.deb ./libnftables1_*.deb \
    ./libip4tc2_*.deb ./libip6tc2_*.deb ./libxtables12_*.deb ./libnetfilter-conntrack3_*.deb \
    ./iptables_*.deb ./runc_*.deb ./containerd_*.deb ./docker.io_*.deb ./docker-compose-v2_*.deb \
    || apt-get -y -f install --no-download || apt-get -y -f install
  rm -rf /tmp/rackloom-debs
'
EOS
chmod 0755 "$PAYLOAD_DST/install-debs.sh"

# Paramètres noyau autoinstall
AUTOINSTALL_ARGS='autoinstall ds=nocloud\;s=/cdrom/nocloud/'
for cfg in "$EXTRACT/boot/grub/grub.cfg" "$EXTRACT/boot/grub/loopback.cfg"; do
  [ -f "$cfg" ] || continue
  if grep -q 'ds=nocloud' "$cfg"; then continue; fi
  # Insère avant le « --- » terminal des entrées linux casper.
  sed -i -E "s|(linux[[:space:]]+/casper/(hwe-)?vmlinuz[[:space:]]*)(---)|\\1${AUTOINSTALL_ARGS} \\3|" "$cfg"
done

# Identifiant de volume ISO 9660 : A–Z, 0–9, underscore, ≤ 32 caractères.
VOLID="RACKLOOM_${STAMP}"

say "reconstruction ISO hybride (BIOS + UEFI, options amont Ubuntu 24.04)"
# Options reprises de `xorriso -report_el_torito as_mkisofs` sur l'ISO 24.04.5 :
# la partition EFI reste lue depuis l'ISO amont (intervalles d'octets figés).
rm -f "$ISO_OUT"
set +e
xorriso -as mkisofs -r \
  -V "$VOLID" \
  -o "$ISO_OUT" \
  --grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:"$ISO_SRC" \
  --protective-msdos-label \
  -partition_cyl_align off \
  -partition_offset 16 \
  --mbr-force-bootable \
  -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b \
    --interval:local_fs:7958876d-7969035d::"$ISO_SRC" \
  -appended_part_as_gpt \
  -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7 \
  -c '/boot.catalog' \
  -b '/boot/grub/i386-pc/eltorito.img' \
  -no-emul-boot \
  -boot-load-size 4 \
  -boot-info-table \
  --grub2-boot-info \
  -eltorito-alt-boot \
  -e '--interval:appended_partition_2_start_1989719s_size_10160d:all::' \
  -no-emul-boot \
  -boot-load-size 10160 \
  "$EXTRACT"
rc=$?
set -e

if [ "$rc" -ne 0 ] || [ ! -s "$ISO_OUT" ]; then
  say "repli reconstruction classique"
  MBR="$WORK/isohdpfx.bin"
  dd if="$ISO_SRC" bs=1 count=432 of="$MBR" status=none
  rm -f "$ISO_OUT"
  xorriso -as mkisofs \
    -r -V "$VOLID" \
    -o "$ISO_OUT" \
    -J -joliet-long \
    --grub2-mbr "$MBR" \
    --protective-msdos-label \
    -partition_offset 16 \
    -b boot/grub/i386-pc/eltorito.img \
    -c boot.catalog \
    -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info \
    -eltorito-alt-boot \
    -e EFI/boot/bootx64.efi \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    "$EXTRACT"
fi

[ -s "$ISO_OUT" ] || { echo "échec de reconstruction ISO" >&2; exit 1; }

say "somme de contrôle et manifeste"
(
  cd "$OUT_DIR"
  sha256sum "$(basename "$ISO_OUT")" | tee "$(basename "$ISO_OUT").sha256"
)

MANIFEST="$ISO_OUT.manifest.txt"
{
  echo "name: $(basename "$ISO_OUT")"
  echo "version_label: $VERSION_LABEL"
  echo "ubuntu_iso: $UBUNTU_ISO_NAME"
  echo "ubuntu_sha256: $EXPECTED"
  echo "git: $GIT_SHA"
  echo "built_at_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "test_seed: $TEST_SEED"
  echo "images_embedded: $EMBED_IMAGES"
  echo "d3_terraform_runner: embedded (legal review open — ADR-0002 / D3)"
  if [ -f "$IMG_STAGE/image-ids.txt" ]; then
    echo "images:"
    sed 's/^/  /' "$IMG_STAGE/image-ids.txt"
  fi
} >"$MANIFEST"

printf '%s\n' "$VERSION_LABEL" >"$ROOT/iso/payload/etc/rackloom/VERSION"

say "terminé"
ls -lh "$ISO_OUT" "$ISO_OUT.sha256" "$MANIFEST"
echo "ISO=$ISO_OUT"
