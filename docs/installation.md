# Installation de l'appliance (ISO)

Installateur exclusif : **Ubuntu Server 24.04 Subiquity / Curtin**, piloté par
autoinstall NoCloud. Les seuls prompts opérateur autorisés passent par les
`interactive-sections` Subiquity (réseau, identity, storage).

## Prérequis matériels

- amd64, ≥ 4 vCPU, ≥ 16 Go RAM recommandés, ≥ 80 Go disque
- UEFI recommandé (prouvé QEMU + OVMF). BIOS legacy / Secure Boot : non annoncés

## Étapes

1. Vérifier l’empreinte : `sha256sum -c rackloom-appliance-*.iso.sha256`
2. Écrire le média (USB `dd` / balenaEtcher, ou montage hyperviseur)
3. Boot UEFI → menu GRUB **Rackloom** → **Install Rackloom appliance**
4. Subiquity :
   - réseau (DHCP ou IPv4 statique)
   - identity : hostname + mot de passe du compte `rackloom`
   - storage : choisir le disque et confirmer (aucun wipe silencieux)
5. Curtin installe Ubuntu ; `late-commands` posent Docker, payload, systemd
6. Reboot automatique — retirer le média ISO
7. Premier boot : voir `docs/first-boot.md`

## Ce qui est interdit

- Installateur texte/GUI Rackloom maison (`iso/installer/`, early-commands UI)
- Partitionnement ou formatage hors Subiquity / Curtin
- Copie du live filesystem vers le disque cible
- Mot de passe ou secret applicatif embarqué dans l’ISO publiée
