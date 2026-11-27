# ISO appliance Rackloom (Lot 3)

Construit une ISO Ubuntu Server 24.04 LTS amd64 reconditionnée (autoinstall, ADR-0004).

## Prérequis

- `xorriso`, `gpg`, `curl`, `sha256sum`
- Docker (construction et sauvegarde des images)
- Pour `make test-iso` : `qemu-system-x86_64`, OVMF (`OVMF_CODE_4M.fd`)

## Commandes

```bash
make iso        # ./iso/build.sh → dist/rackloom-appliance-*.iso
make test-iso   # install QEMU/UEFI + reboot sans média
```

Variables utiles :

| Variable | Rôle |
|---|---|
| `RACKLOOM_ISO_CACHE` | cache de l'ISO amont (défaut `/tmp/rackloom-iso-cache`) |
| `RACKLOOM_ISO_OUT` | répertoire de sortie (défaut `dist/`) |
| `RACKLOOM_ISO_SKIP_IMAGES=1` | n'embarque pas les images Docker |
| `RACKLOOM_ISO_DEB_DIR` | répertoire de `.deb` Docker pré-téléchargés |
| `RACKLOOM_ISO_EMBED_IMAGES=0` | alias de skip images |

## Contenu embarqué

- Graine autoinstall (`nocloud/`) : stockage **interactif** en production
- Payload `/rackloom/payload` → `/` (systemd, firstboot, Compose appliance)
- Paquets Docker hors ligne (`/rackloom/debs`)
- Images `rackloom/*:appliance` + `postgres:16.11-bookworm` (sauf skip)

## Licence Terraform (D3)

Le runner `rackloom/runner-terraform:appliance` embarque le binaire Terraform HashiCorp (BSL 1.1), déjà utilisé en développement. La **redistribution** dans une ISO publiée reste soumise à l'avis juridique (décision D3 ouverte). Ne pas diffuser l'artefact hors laboratoire tant que D3 n'est pas tranchée.
