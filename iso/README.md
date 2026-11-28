# ISO appliance Rackloom (Lot 3)

ISO Ubuntu Server 24.04 LTS amd64 remasterée avec un **installateur TUI Rackloom**.

Contrainte produit : **pas** Subiquity / Curtin UI / autoinstall comme parcours d'installation.
Ubuntu 24.04 est posé par **mmdebstrap/debootstrap** après partitionnement GPT.

## Commandes

```bash
make validate
make build-iso                 # ou make iso ; Windows : .\iso\build.ps1
make test-iso                  # QEMU/OVMF — Étape 1–2
make test-qemu-install
```

Graine test : `./iso/build.sh --test-seed` (mode auto `/dev/vda`).

## Parcours opérateur

1. GRUB **Install Rackloom appliance**
2. TUI Rackloom — Étape 1 : disque (confirmation)
3. Étape 2 : partition + bootstrap Ubuntu 24.04 + GRUB
4. Message **Installation Rackloom terminée** → reboot (retirer l'ISO)
5. Étapes suivantes (réseau, hostname, mot de passe, payload) : à enchaîner

## Contenu

- `/rackloom/installer/` — TUI réel (pas early-commands Subiquity)
- Live squashfs : Subiquity masqué, `rackloom-installer.service`
- Payload appliance + firstboot (étapes ultérieures)
- Branding GRUB Rackloom
