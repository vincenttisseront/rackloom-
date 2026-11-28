# Tests — ISO et appliance

## Validation statique (obligatoire CI)

```bash
make validate          # ./iso/scripts/validate-autoinstall.sh
make test              # inclut validate + branding GRUB
```

`validate` échoue si :

- `iso/installer/` existe
- `user-data` contient `early-commands` ou un password embarqué
- `interactive-sections` sans `storage` / `identity`
- `build.sh` / Makefile réintroduisent l’installateur maison

## Construction

```bash
make build-iso         # ou make iso ; Windows : .\iso\build.ps1
```

Sans Docker images : `RACKLOOM_ISO_SKIP_IMAGES=1 make build-iso` (preuve remaster uniquement).

## QEMU / OVMF

```bash
make test-iso          # ou test-qemu-install / test-qemu-reboot
# équivalent : ./iso/test.sh [chemin.iso]
```

Phases du script :

1. Install autoinstall non interactive (`user-data.test`, `/dev/vda`)
2. Arrêt, retrait ISO
3. Reboot disque seul — signes `rackloom-firstboot` / login Ubuntu 24.04

Prérequis : `qemu-system-x86_64`, `OVMF_CODE_4M.fd`, `OVMF_VARS_4M.fd`.

Niveau de preuve : **testé localement** quand le script sort 0 ; **validé en laboratoire** seulement après session réelle consignable dans `docs/progress.md`.
