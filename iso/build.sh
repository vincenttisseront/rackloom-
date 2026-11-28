#!/usr/bin/env bash
# Construit l'ISO appliance Rackloom à partir de l'ISO Ubuntu Server 24.04 LTS
# (vérifiée SHA-256 + signature GPG) — ADR-0004.
#
# Usage :
#   ./iso/build.sh              # ISO de production (stockage interactif)
#   ./iso/build.sh --test-seed  # graine autoinstall non interactive (CI / test-iso)
#   RACKLOOM_ISO_SKIP_IMAGES=1 ./iso/build.sh
#   RACKLOOM_ISO_IMAGE_DIR=/path/to/tars ./iso/build.sh  # images déjà sauvées (Windows)
#
# Sous Windows (Docker Desktop, sans WSL) : iso/build.ps1
#
# Sortie : dist/rackloom-appliance-<version>-amd64.iso (+ .sha256 + .manifest.txt)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# Filet CRLF (montage Windows) : source une copie sans CR.
# shellcheck source=versions.env
source <(sed 's/\r$//' "$ROOT/iso/versions.env")

TEST_SEED=0
EMBED_IMAGES="${RACKLOOM_ISO_EMBED_IMAGES:-1}"
SKIP_IMAGES="${RACKLOOM_ISO_SKIP_IMAGES:-0}"
[ "$SKIP_IMAGES" = 1 ] && EMBED_IMAGES=0

for arg in "$@"; do
  case "$arg" in
    --test-seed) TEST_SEED=1 ;;
    --skip-images) EMBED_IMAGES=0 ;;
    -h|--help)
      sed -n '2,14p' "$0"
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
# docker n'est requis que si l'on construit/sauve les images ici (pas via RACKLOOM_ISO_IMAGE_DIR).
if [ -z "${RACKLOOM_ISO_IMAGE_DIR:-}" ] && [ "$EMBED_IMAGES" = 1 ]; then
  need docker
fi

CACHE="${RACKLOOM_ISO_CACHE:-${TMPDIR:-/tmp}/rackloom-iso-cache}"
WORK="${RACKLOOM_ISO_WORK:-${TMPDIR:-/tmp}/rackloom-iso-work}"
OUT_DIR="${RACKLOOM_ISO_OUT:-$ROOT/dist}"
mkdir -p "$CACHE" "$WORK" "$OUT_DIR"

STAMP=$(date -u +%Y%m%d)
GIT_SHA="${RACKLOOM_ISO_GIT_SHA:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
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

if [ -n "${RACKLOOM_ISO_IMAGE_DIR:-}" ]; then
  # Chemin Windows (build.ps1) : images déjà construites/sauvegardées sur l'hôte.
  say "images pré-construites depuis $RACKLOOM_ISO_IMAGE_DIR"
  for name in backend launcher web runner-ansible runner-terraform postgres; do
    [ -f "$RACKLOOM_ISO_IMAGE_DIR/$name.tar" ] || {
      echo "archive image absente: $RACKLOOM_ISO_IMAGE_DIR/$name.tar" >&2
      exit 1
    }
  done
  cp -a "$RACKLOOM_ISO_IMAGE_DIR/." "$IMG_STAGE/"
  if [ ! -f "$IMG_STAGE/manifest.sha256" ]; then
    (cd "$IMG_STAGE" && sha256sum ./*.tar >manifest.sha256)
  fi
elif [ "$EMBED_IMAGES" = 1 ]; then
  say "construction / sauvegarde des images :$RACKLOOM_IMAGE_TAG"
  export DOCKER_BUILDKIT=1
  # --network=host accélère sur Linux natif ; Docker Desktop (Windows) ne le gère pas.
  # RACKLOOM_ISO_DOCKER_NETWORK=host|bridge|… (vide = défaut du démon).
  NET_ARGS=()
  if [ -n "${RACKLOOM_ISO_DOCKER_NETWORK:-}" ]; then
    NET_ARGS=(--network="$RACKLOOM_ISO_DOCKER_NETWORK")
  elif [ "$(uname -s)" = Linux ] && [ -z "${DOCKER_DESKTOP:-}" ]; then
    NET_ARGS=(--network=host)
  fi
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
    # apt-get download suffit avec les index existants (hôte Linux).
    # Conteneur builder frais : pas de listes → update (root) puis download.
    if ! ls /var/lib/apt/lists/*_Packages >/dev/null 2>&1; then
      apt-get update -qq
    fi
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

say "injection installateur Rackloom + payload (pas d'autoinstall UX)"
PAYLOAD_DST="$EXTRACT/rackloom"
rm -rf "$PAYLOAD_DST"
mkdir -p "$PAYLOAD_DST/payload" "$PAYLOAD_DST/debs" "$PAYLOAD_DST/installer"
cp -a "$ROOT/iso/payload/." "$PAYLOAD_DST/payload/"
printf '%s\n' "$VERSION_LABEL" >"$PAYLOAD_DST/payload/etc/rackloom/VERSION"
cp -a "$DEB_STAGE/." "$PAYLOAD_DST/debs/"
# Installateur TUI réel (partition + mmdebstrap) — pas early-commands Subiquity.
cp -a "$ROOT/iso/installer/." "$PAYLOAD_DST/installer/"
chmod 0755 "$PAYLOAD_DST/installer/"*.sh
# Lien live : /rackloom → contenu ISO (casper monte /cdrom ; on expose aussi à la racine live via squashfs).
mkdir -p "$PAYLOAD_DST/payload/opt/rackloom/images"
if [ "$EMBED_IMAGES" = 1 ]; then
  cp -a "$IMG_STAGE/." "$PAYLOAD_DST/payload/opt/rackloom/images/"
else
  cp -a "$IMG_STAGE/README.txt" "$PAYLOAD_DST/payload/opt/rackloom/images/" 2>/dev/null || true
fi

# Paquets outils installateur (mmdebstrap, gdisk, …) pour le live.
TOOL_DEB_STAGE="$WORK/tool-debs"
rm -rf "$TOOL_DEB_STAGE"
mkdir -p "$TOOL_DEB_STAGE" "$PAYLOAD_DST/tool-debs"
if [ -d "${RACKLOOM_ISO_TOOL_DEB_DIR:-}" ]; then
  cp -a "${RACKLOOM_ISO_TOOL_DEB_DIR}/." "$TOOL_DEB_STAGE/"
else
  (
    cd "$TOOL_DEB_STAGE"
    if ! ls /var/lib/apt/lists/*_Packages >/dev/null 2>&1; then
      if [ "$(id -u)" -eq 0 ]; then apt-get update -qq; else sudo apt-get update -qq; fi
    fi
    DL=(apt-get download)
    if [ "$(id -u)" -ne 0 ]; then DL=(sudo apt-get download); fi
    "${DL[@]}" mmdebstrap gdisk parted dosfstools e2fsprogs || true
    # apt-get download as root may leave root-owned files
    sudo chown -R "$(id -u):$(id -g)" "$TOOL_DEB_STAGE" 2>/dev/null || true
  )
fi
if ls "$TOOL_DEB_STAGE"/*.deb >/dev/null 2>&1; then
  cp -a "$TOOL_DEB_STAGE"/. "$PAYLOAD_DST/tool-debs/"
fi

# Retirer toute graine autoinstall éventuelle (pas d'UX Subiquity).
rm -rf "$EXTRACT/nocloud"

# Kernel : masquer Subiquity + console série (QEMU) ; pas d'autoinstall.
KERNEL_ARGS='layerfs-path=ubuntu-server-minimal.ubuntu-server.installer.rackloom.squashfs systemd.mask=snap.subiquity.subiquity-server.service systemd.mask=snap.subiquity.subiquity-service.service systemd.mask=subiquity.service console=ttyS0 console=tty0'
for cfg in "$EXTRACT/boot/grub/grub.cfg" "$EXTRACT/boot/grub/loopback.cfg"; do
  [ -f "$cfg" ] || continue
  if grep -q 'systemd.mask=subiquity' "$cfg"; then continue; fi
  sed -i -E "s|(linux[[:space:]]+/casper/(hwe-)?vmlinuz[[:space:]]*)(---)|\\1${KERNEL_ARGS} \\3|" "$cfg"
done

# Branding menu GRUB / splash (remplace « Try or Install Ubuntu Server »).
say "branding GRUB Rackloom"
bash "$ROOT/iso/branding/apply-grub-branding.sh" "$EXTRACT" "$ROOT/iso/branding"

# Live squashfs : activer rackloom-installer, masquer Subiquity, outils bootstrap.
say "overlay live rackloom (layerfs, masque Subiquity)"
need unsquashfs
need mksquashfs
OVERLAY_DIR="$WORK/rackloom-overlay"
rm -rf "$OVERLAY_DIR"
mkdir -p "$OVERLAY_DIR/etc/systemd/system/multi-user.target.wants" \
  "$OVERLAY_DIR/etc/systemd/system/serial-getty@.service.d" \
  "$OVERLAY_DIR/usr/lib/systemd/system/serial-getty@.service.d" \
  "$OVERLAY_DIR/rackloom/installer" \
  "$OVERLAY_DIR/rackloom/tool-debs"

# Masquer les unités snap Subiquity (Ubuntu 24.04 live-server).
for unit in snap.subiquity.subiquity-server.service snap.subiquity.subiquity-service.service \
  subiquity.service subiquity-server.service subiquity-service.service; do
  ln -sfn /dev/null "$OVERLAY_DIR/etc/systemd/system/$unit"
done

# Empêcher serial-getty de lancer Subiquity ; getty classique.
cat >"$OVERLAY_DIR/usr/lib/systemd/system/serial-getty@.service.d/subiquity-serial.conf" <<'EOV'
[Unit]
Description=Serial Getty on %I (Rackloom — pas Subiquity)
StartLimitInterval=0

[Service]
Environment=
ExecStart=
ExecStart=-/sbin/agetty -o '-p -- \\u' --keep-baud 115200,57600,38400,9600 %I $TERM
EOV

cp -a "$PAYLOAD_DST/installer/." "$OVERLAY_DIR/rackloom/installer/"
chmod 0755 "$OVERLAY_DIR/rackloom/installer/"*.sh
if ls "$PAYLOAD_DST/tool-debs"/*.deb >/dev/null 2>&1; then
  cp -a "$PAYLOAD_DST/tool-debs/." "$OVERLAY_DIR/rackloom/tool-debs/"
fi
if [ -x /usr/bin/mmdebstrap ]; then
  mkdir -p "$OVERLAY_DIR/usr/bin"
  cp -a /usr/bin/mmdebstrap "$OVERLAY_DIR/usr/bin/mmdebstrap"
fi
cp -a "$ROOT/iso/installer/systemd/rackloom-installer.service" \
  "$OVERLAY_DIR/etc/systemd/system/rackloom-installer.service"
ln -sfn /etc/systemd/system/rackloom-installer.service \
  "$OVERLAY_DIR/etc/systemd/system/multi-user.target.wants/rackloom-installer.service"

# Lien /cdrom/rackloom/tool-debs pour ensure_tools (cdrom monté séparément ; copie aussi sous /rackloom).
OVERLAY_SQ="$EXTRACT/casper/ubuntu-server-minimal.ubuntu-server.installer.rackloom.squashfs"
mksquashfs "$OVERLAY_DIR" "$OVERLAY_SQ" -comp xz -noappend
rm -rf "$OVERLAY_DIR"
say "overlay écrit : $(basename "$OVERLAY_SQ")"

if [ "$TEST_SEED" = 1 ]; then
  mkdir -p "$EXTRACT/rackloom"
  cat >"$EXTRACT/rackloom/installer.env" <<'EOF'
RACKLOOM_INSTALLER_AUTO=1
RACKLOOM_DISK=/dev/vda
EOF
  for cfg in "$EXTRACT/boot/grub/grub.cfg" "$EXTRACT/boot/grub/loopback.cfg"; do
    [ -f "$cfg" ] || continue
    sed -i -E 's/^set timeout=.*/set timeout=0/' "$cfg"
    sed -i -E 's/^terminal_output gfxterm/terminal_output console/' "$cfg"
  done
fi

if [ -f "$EXTRACT/md5sum.txt" ]; then
  (
    cd "$EXTRACT"
    find casper -type f -print0 | sort -z | xargs -0 md5sum >md5sum.txt
  ) || true
fi

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
