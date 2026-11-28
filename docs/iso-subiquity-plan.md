# Plan — retour à Subiquity / Curtin officiel (RAC-62)

Contrainte absolue Vincent (2026-11-28) : l’ISO appliance **doit** utiliser l’installateur officiel Ubuntu Server 24.04 (Subiquity/Curtin). Interdit : installateur texte/GUI maison, copie live FS → disque, partitionnement hors Subiquity, alternative homemade à Subiquity.

Préférence projet alignée : installation via Subiquity/Curtin + `autoinstall` / cloud-init NoCloud / `late-commands` / firstboot systemd uniquement.

## Constat sur `main` (post-#52)

Merge `6142b68` (PR #52, `cursor/iso-install-custom-7f9a`) introduit un **installateur console maison** qui viole la contrainte :

| Chemin | Rôle illicite |
|---|---|
| `iso/installer/run.sh` | UI console early-commands (réseau, hostname, password, disque) |
| `iso/installer/lib.sh` | helpers TTY / hash / listing disques |
| `iso/installer/render-autoinstall.sh` | réécriture de `/autoinstall.yaml` hors Subiquity interactif |
| `iso/installer/autoinstall.template.yaml` | modèle post-collecte (storage match path maison) |
| `iso/installer/test-installer.sh` | smoke du chemin maison (appelé par `make test`) |
| `iso/autoinstall/user-data` | bootstrap `early-commands: bash …/run.sh` + stubs disque fail-closed |
| `iso/build.sh` | copie `iso/installer/` → `/cdrom/rackloom/installer/` |
| `docs/adr/0004-…`, `iso/README.md`, `docs/progress.md` | documentent l’architecture console maison |
| Magasin : `docs/iso-deploiement.md`, `internal/rapport-iso-install-custom.md` | parcours opérateur RAC-61 |

Conservé (compatible) :

- Branding GRUB Rackloom (`iso/branding/`) — menu/splash, pas un installateur
- Payload + firstboot systemd (`iso/payload/…`)
- Graine test `user-data.test` (CI / QEMU, non interactive, `/dev/vda`)
- `iso/build.ps1` / `build.cmd` (Windows Docker Desktop) — à garder, sans embarquer `installer/`

## Architecture cible (unique)

```
ISO Ubuntu 24.04.5 live-server (SHA-256 + GPG)
  → remaster xorriso
  → nocloud/user-data  (autoinstall officiel)
  → GRUB branding Rackloom (entrée « Install Rackloom appliance »)
  → Subiquity/Curtin : partitionnement + identity (+ network si interactif)
  → late-commands : debs Docker, payload, enable systemd
  → reboot → rackloom-firstboot.service (idempotent)
```

**Aucun** second chemin d’installation. Pas de dualité console maison + Subiquity.

### Graine production (`iso/autoinstall/user-data`)

- `autoinstall.version: 1`
- `interactive-sections: [storage, identity]` (et `network` si on veut le prompt réseau via Subiquity)
- Pas de mot de passe en clair / hash dans l’ISO publiée — Subiquity identity le demande
- Storage : layout LVM ; **choix et confirmation disque = Subiquity** (pas de `match.path` maison)
- `late-commands` inchangés dans l’esprit (install-debs, cp payload, enable units)
- Pas de `early-commands` installateur

### Graine test (`iso/autoinstall/user-data.test`)

- 100 % non interactive pour QEMU (`/dev/vda`, hash lab uniquement)
- Marqueur `/var/lib/rackloom/.iso-test-installed`
- Jamais publiée comme ISO de production

## Plan d’implémentation (minimal)

1. **Supprimer** tout le dossier `iso/installer/` et retirer son embarquement de `build.sh`.
2. **Restaurer** `user-data` Subiquity interactif (baseline pré-#52, éventuellement + `network`).
3. **Retirer** `./iso/installer/test-installer.sh` de `make test` ; ajouter `make validate` qui :
   - refuse la présence de `iso/installer/` ou de `early-commands` maison ;
   - vérifie `interactive-sections` + absence de secrets en clair ;
   - vérifie `versions.env` (ISO amont + URL SHA256).
4. **Alias Make** : `build-iso` → `iso` ; `test-qemu-install` / `test-qemu-reboot` → phases de `iso/test.sh` (ou `test-iso` inchangé + wrappers).
5. **Firstboot** : conserver l’idempotence ; `install-hostname` optionnel (Subiquity pose déjà le hostname) — pas de dépendance à l’installateur maison.
6. **Docs dépôt** : `architecture`, `iso-build`, `installation`, `first-boot`, `security-model`, `testing`, `progress`, ADR-0004, `iso/README`, **CLAUDE.md** (règle invariante Subiquity).
7. **Magasin projet** : ce plan ; mettre à jour `docs/iso-deploiement.md` ; rapport `internal/rapport-iso-subiquity.md`.
8. **Preuves** : `make validate` ; rebuild ISO (xorriso, skip-images OK si Docker absent) ; `make test-iso` / `test-qemu-*` si QEMU+OVMF disponibles.
9. **Une PR**, CI verte, **pas de merge**.

## Hors scope

- Lot 4 / OIDC
- D3 (Terraform BSL) / D5 (Podman)
- Refonte branding GRUB (déjà OK)
- Changement du payload Compose au-delà du nécessaire

## Critères d’acceptation

- [ ] Aucun fichier sous `iso/installer/`
- [ ] `user-data` sans `early-commands` console ; storage via Subiquity
- [ ] `user-data.test` non interactif pour QEMU
- [ ] `build.ps1` fonctionne sans chemin installateur
- [ ] Docs + CLAUDE.md affirment Subiquity exclusif
- [ ] `make validate` + build ISO + test QEMU documentés dans le rapport
- [ ] Une PR ouverte, non mergée
