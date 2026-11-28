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

Sous PowerShell 5.1, l’absence d’une image `:appliance` ne doit **pas** arrêter le script :
`Test-DockerImage` lit `$LASTEXITCODE` avec `ErrorActionPreference = Continue`
(le stderr de `docker image inspect` est sinon terminant). Le fichier est UTF-8 avec BOM.

Fins de ligne : `.gitattributes` force `eol=lf` sous `iso/**` (sauf `.cmd`).
`build.ps1` normalise aussi en LF avant le remaster ; l’entrypoint du conteneur
(`iso/docker-entrypoint.sh`) retire les `\r` restants (filet montage Windows).

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

- Graine autoinstall (`nocloud/`) : bootstrap `early-commands` → installateur console Rackloom
- Installateur console (`/rackloom/installer/`) : **seuls** réseau, hostname, mot de passe, confirmation disque
- Payload `/rackloom/payload` → `/` (systemd, firstboot, Compose appliance)
- Paquets Docker hors ligne (`/rackloom/debs`)
- Images `rackloom/*:appliance` + `postgres:16.11-bookworm` (sauf skip)
- Branding GRUB (`iso/branding/`) : menu « Install Rackloom appliance », splash / thème

### Branding boot (GRUB)

Au démarrage de l’ISO (UEFI/BIOS), l’opérateur doit voir :

- fond / splash Rackloom (slate `#0f172a`, barre accent bleu)
- titre de thème **Rackloom**
- entrée principale **Install Rackloom appliance** (plus « Try or Install Ubuntu Server »)
- variante **Install Rackloom appliance (safe graphics)** si présente amont

Smoke hors ISO : `./iso/branding/test-apply-branding.sh` (inclus dans `make test`).

### Installateur console (pas d’UI Ubuntu)

Après le menu GRUB, l’opérateur voit **uniquement** l’installateur Rackloom (`iso/installer/run.sh`) :

1. Réseau (DHCP ou IPv4 statique)
2. Hostname / nom
3. Mot de passe du compte `rackloom` (username figé)
4. Choix du disque + retaper le chemin + confirmation finale (aucun wipe silencieux)

Aucune fenêtre Subiquity stock (langue, clavier, miroir, source, mises à jour, snaps, Ubuntu Pro, SSH…). L’installateur réécrit `/autoinstall.yaml` ; Subiquity/curtin enchaînent en non interactif.

| Couche | Rôle |
|---|---|
| Opérateur | GRUB Rackloom + invites console Rackloom |
| Sous le capot | Ubuntu 24.04 live-server, Subiquity, curtin, casper |

Smoke hors ISO : `./iso/installer/test-installer.sh` (inclus dans `make test`).

Graine CI (`--test-seed` / `user-data.test`) : entièrement non interactive, **sans** l’UI console (disque `/dev/vda`).

Rebuild requis pour le média : `make iso` ou `.\iso\build.ps1`.

## Licence Terraform (D3)

Le runner `rackloom/runner-terraform:appliance` embarque le binaire Terraform HashiCorp (BSL 1.1), déjà utilisé en développement. La **redistribution** dans une ISO publiée reste soumise à l'avis juridique (décision D3 ouverte). Ne pas diffuser l'artefact hors laboratoire tant que D3 n'est pas tranchée.
