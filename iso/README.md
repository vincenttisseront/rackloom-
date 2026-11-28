# ISO appliance Rackloom (Lot 3)

Construit une ISO Ubuntu Server 24.04 LTS amd64 reconditionnée autour de
**Subiquity / Curtin officiels** + autoinstall NoCloud (ADR-0004).

Contrainte produit : **aucun** installateur console/GUI maison, aucun
partitionnement hors Subiquity, aucune copie live FS → disque.

## Prérequis

### Linux (ou WSL avec outils natifs)

- `xorriso`, `gpg`, `curl`, `sha256sum`
- Docker (construction et sauvegarde des images)
- Pour `make test-iso` / `test-qemu-*` : `qemu-system-x86_64`, OVMF (`OVMF_CODE_4M.fd`)

### Windows (poste natif, sans distro WSL)

- **Docker Desktop** démarré en mode **Linux containers**
- PowerShell 5.1+ (Windows) ou PowerShell 7+
- Accès réseau à `releases.ubuntu.com` (sauf cache local)
- **Pas** d’installation locale de `xorriso` / `gpg` / `apt` : ces étapes tournent dans l’image `rackloom/iso-builder:appliance`

## Commandes

### Linux

```bash
make validate            # refuse tout chemin installateur maison
make build-iso           # alias de make iso → ./iso/build.sh
make iso                 # même chose
make test-iso            # install QEMU/UEFI + reboot sans média
make test-qemu-install   # alias
make test-qemu-reboot    # alias (même script : install puis reboot)
```

### Windows (Docker Desktop)

```powershell
.\iso\build.ps1
.\iso\build.cmd
.\iso\build.ps1 -SkipImages
.\iso\build.ps1 -TestSeed      # graine autoinstall non interactive (QEMU)
```

`build.ps1` construit les images `:appliance` sur le démon Desktop, puis lance
`iso/build.sh` dans le conteneur builder (gpg, apt `.deb`, xorriso). Sortie :
`dist/rackloom-appliance-*.iso` (+ `.sha256`, `.manifest.txt`).

Fins de ligne : `.gitattributes` force `eol=lf` sous `iso/**` (sauf `.cmd`).

## Contenu embarqué

- Graine autoinstall NoCloud (`nocloud/`) — Subiquity officiel
- Payload `/rackloom/payload` → `/` (systemd, firstboot, Compose appliance)
- Paquets Docker hors ligne (`/rackloom/debs`)
- Images `rackloom/*:appliance` + `postgres:16.11-bookworm` (sauf skip)
- Branding GRUB (`iso/branding/`) : menu « Install Rackloom appliance », splash

### Parcours opérateur (Subiquity)

1. Splash / menu GRUB **Rackloom** → **Install Rackloom appliance**
2. Subiquity : réseau (si besoin), identity (hostname + mot de passe `rackloom`), **storage** (choix + confirmation disque)
3. Curtin applique le plan ; `late-commands` posent payload + unités
4. Reboot → `rackloom-firstboot` (TLS, secrets, jeton) — idempotent

Smoke hors ISO : `./iso/branding/test-apply-branding.sh` et
`./iso/scripts/validate-autoinstall.sh` (inclus dans `make test` / `make validate`).

Graine CI (`--test-seed` / `user-data.test`) : entièrement non interactive
(disque `/dev/vda`) — **jamais** publiée.

## Licence Terraform (D3)

Le runner `rackloom/runner-terraform:appliance` embarque le binaire Terraform
HashiCorp (BSL 1.1). Redistribution hors laboratoire soumise à l’avis juridique (D3).
