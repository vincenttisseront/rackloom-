# Construction de l'ISO appliance

Voir aussi : `iso/README.md`, ADR-0004, `docs/installation.md`, `docs/testing.md`.

## Principe

L’ISO est un **remaster** de l’ISO Ubuntu Server 24.04 LTS amd64 officielle
(`live-server`), vérifiée via `SHA256SUMS` + signature GPG
(`iso/versions.env` : nom, URL, clé `UBUNTU_CD_SIGNING_KEY`).

Le remaster ajoute uniquement :

1. Graine **autoinstall** NoCloud (`iso/autoinstall/`) pour **Subiquity / Curtin**
2. Payload Rackloom + paquets Docker hors ligne + images `:appliance`
3. Branding GRUB (`iso/branding/`)

**Interdit** dans le pipeline de build : installateur maison, early-commands UI,
partitionnement hors Subiquity, dualité de chemins d’installation.

## Commandes

| Cible | Rôle |
|---|---|
| `make validate` | Refuse `iso/installer/`, vérifie `user-data` Subiquity |
| `make build-iso` / `make iso` | Télécharge ISO amont, remaster xorriso → `dist/` |
| `.\iso\build.ps1` | Même résultat sous Windows (Docker Desktop) |

Variables utiles : `RACKLOOM_ISO_CACHE`, `RACKLOOM_ISO_OUT`,
`RACKLOOM_ISO_SKIP_IMAGES=1`, `RACKLOOM_ISO_DEB_DIR`, `RACKLOOM_ISO_IMAGE_DIR`.

## Sorties

- `dist/rackloom-appliance-24.04.5-<YYYYMMDD>-amd64.iso`
- `.sha256` et `.manifest.txt` (empreintes Ubuntu + images embarquées)

Graine `--test-seed` : suffixe `-test-amd64.iso` (CI / QEMU uniquement).
