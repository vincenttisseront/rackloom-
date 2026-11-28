# ISO appliance Rackloom (Lot 3)

Construit une ISO Ubuntu Server 24.04 LTS amd64 reconditionnée (autoinstall, ADR-0004).

## Prérequis

### Linux (ou WSL avec outils natifs)

- `xorriso`, `gpg`, `curl`, `sha256sum`
- Docker (construction et sauvegarde des images)
- Pour `make test-iso` : `qemu-system-x86_64`, OVMF (`OVMF_CODE_4M.fd`)

### Windows (poste natif, sans distro WSL)

- **Docker Desktop** démarré en mode **Linux containers** (pas Windows containers)
- PowerShell 5.1+ (Windows) ou PowerShell 7+
- Accès réseau à `releases.ubuntu.com` (téléchargement ISO amont + signature GPG), sauf cache local
- **Pas** d’installation locale de `xorriso` / `gpg` / `apt` : ces étapes tournent dans l’image `rackloom/iso-builder:appliance` (`iso/Dockerfile.builder`)

## Commandes

### Linux

```bash
make iso        # ./iso/build.sh → dist/rackloom-appliance-*.iso
make test-iso   # install QEMU/UEFI + reboot sans média
```

### Windows (Docker Desktop)

Depuis la racine du dépôt, en PowerShell :

```powershell
.\iso\build.ps1
# ou
.\iso\build.cmd

.\iso\build.ps1 -SkipImages    # sans embarquer les images Docker
.\iso\build.ps1 -TestSeed      # graine autoinstall non interactive
```

Déroulement de `build.ps1` :

1. Vérifie `docker version` (Server.Os = `linux`)
2. Construit l’image builder `rackloom/iso-builder:appliance`
3. Construit / sauve les images `:appliance` sur le démon Desktop (équivalent `build.sh`)
4. Lance un conteneur Linux qui exécute `iso/build.sh` (gpg, apt `.deb`, xorriso)
5. Écrit `dist/rackloom-appliance-*.iso`, `.sha256` et `.manifest.txt` sur le FS Windows

Variables utiles :

| Variable | Rôle |
|---|---|
| `RACKLOOM_ISO_CACHE` | cache de l'ISO amont (Linux : `/tmp/rackloom-iso-cache` ; Windows : `%LOCALAPPDATA%\rackloom-iso-cache`) |
| `RACKLOOM_ISO_OUT` | répertoire de sortie (défaut `dist/`) |
| `RACKLOOM_ISO_WORK` | répertoire de travail (Windows : `%TEMP%\rackloom-iso-work`) |
| `RACKLOOM_ISO_SKIP_IMAGES=1` | n'embarque pas les images Docker |
| `RACKLOOM_ISO_DEB_DIR` | répertoire de `.deb` Docker pré-téléchargés |
| `RACKLOOM_ISO_EMBED_IMAGES=0` | alias de skip images |
| `RACKLOOM_ISO_IMAGE_DIR` | archives `.tar` déjà sauvées (utilisé par `build.ps1`) |
| `RACKLOOM_ISO_DOCKER_NETWORK` | réseau `docker build` (défaut `host` sur Linux natif ; omis sous Docker Desktop) |
| `RACKLOOM_ISO_GIT_SHA` | empreinte courte forcée dans le manifeste |

## Contenu embarqué

- Graine autoinstall (`nocloud/`) : stockage **interactif** en production
- Payload `/rackloom/payload` → `/` (systemd, firstboot, Compose appliance)
- Paquets Docker hors ligne (`/rackloom/debs`)
- Images `rackloom/*:appliance` + `postgres:16.11-bookworm` (sauf skip)

## Licence Terraform (D3)

Le runner `rackloom/runner-terraform:appliance` embarque le binaire Terraform HashiCorp (BSL 1.1), déjà utilisé en développement. La **redistribution** dans une ISO publiée reste soumise à l'avis juridique (décision D3 ouverte). Ne pas diffuser l'artefact hors laboratoire tant que D3 n'est pas tranchée.
