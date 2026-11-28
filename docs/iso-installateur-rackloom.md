# Plan — installateur Rackloom personnalisé (RAC-62 corrigé)

Contrainte Vincent (2026-11-28, **corrigée**) : l’ISO appliance boote sur un **installateur TUI Rackloom**.  
**Interdit comme UX** : Subiquity, écrans Ubuntu Server, Curtin UI, `autoinstall.yaml` comme parcours d’installation.

Ubuntu 24.04 LTS est installé **par** l’installateur Rackloom via **mmdebstrap/debootstrap** (pas de copie live FS → disque comme méthode d’install).

Préférence projet alignée (magasin) : installateur personnalisé TUI/WebUI ; pas Subiquity ni autoinstall comme UX.

## Écart vs #52

| #52 (early-commands) | Cible |
|---|---|
| Collecte invites puis réécrit `/autoinstall.yaml` | Installateur **réel** (partition + bootstrap) |
| Subiquity/Curtin font l’install | **Aucun** Subiquity dans le parcours |
| UI console = façade | UI console = cœur du produit |

Réutiliser de #52 : helpers TTY, listing disques, confirmation explicite, branding GRUB — **pas** le template autoinstall ni early-commands.

## Architecture

```
ISO Ubuntu live-server 24.04 (SHA-256 + GPG) remasterée
  → GRUB Rackloom (« Install Rackloom appliance »)
  → live casper SANS Subiquity (services masqués)
  → systemd : rackloom-installer.service
  → TUI Rackloom (/rackloom/installer/)
  → partitionnement + mmdebstrap noble + GRUB
  → reboot sans ISO → message « Installation Rackloom terminée »
  → (étapes suivantes) réseau / hostname / mot de passe / payload / firstboot
```

Chemin unique : pas de dualité Subiquity + maison.

## Étapes produit

| Étape | Contenu | Preuve minimale |
|---|---|---|
| **1** | Détection disques, choix, confirmation (retaper le chemin) | TUI / mode `--auto` QEMU |
| **2** | Partition GPT (ESP + root), mmdebstrap Ubuntu 24.04, GRUB, reboot, message fin | QEMU install + reboot sans ISO |
| **3+** | Réseau, hostname, mot de passe `rackloom`, payload appliance, firstboot systemd | enchaînement après 1–2 |

Cette livraison : **Étape 1–2** (chemin QEMU minimal vérifiable), squelette prêt pour 3+.

## Implémentation technique

1. **Live root** : décompresser `casper/filesystem.squashfs`, masquer `subiquity*`, activer `rackloom-installer.service`, tools (parted/sgdisk, mmdebstrap ou `.deb` sur `/cdrom`).
2. **`iso/installer/`** :
   - `run.sh` — point d’entrée TUI / `--auto`
   - `lib.sh` — TTY, disques, confirmations
   - `partition.sh` — GPT ESP+root (UEFI)
   - `bootstrap.sh` — mmdebstrap/debootstrap noble → `/mnt`
   - `grub-install.sh` — GRUB EFI + fallback
   - `finish.sh` — motd/issue + unité « Installation Rackloom terminée », reboot
   - `test-installer.sh` — smoke hors ISO
3. **`build.sh`** : embarque installer + outils ; **pas** de graine autoinstall UX ; pas d’`early-commands` Subiquity. `build.ps1` inchangé (appelle `build.sh`).
4. **Mode test** : `RACKLOOM_INSTALLER_AUTO=1` / `--auto --disk /dev/vda` (pas de prompts).
5. **Docs** : ADR-0004, CLAUDE.md invariant 28, iso-build/installation/testing, progress ; révoquer le plan Subiquity.

## Preuves

- `make validate` : refuse autoinstall UX / early-commands Subiquity ; exige `iso/installer/`
- `make build-iso` (skip-images OK)
- `make test-qemu-install` : install jusqu’au message + reboot sans média

## Hors scope immédiat

- WebUI d’install, Secure Boot annoncé, D3/D5
- Payload Docker complet sur la cible (peut suivre en étape 3+)

## Critères d’acceptation

- [ ] Boot → TUI Rackloom (aucun écran Subiquity)
- [ ] Étape 1–2 QEMU : disque → partition → bootstrap → GRUB → reboot → message
- [ ] Pas d’`autoinstall.yaml` comme parcours opérateur
- [ ] `build.ps1` compatible
- [ ] Une PR, CI verte, pas de merge
