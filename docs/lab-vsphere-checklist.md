# Checklist laboratoire vSphere (à remplir quand Vincent autorise)

Ce document est un **runbook futur**. Sous D2, aucun laboratoire réel n'est autorisé : ne pas y écrire d'hôte, de compte, d'adresse ou de secret. Les cases restent vides jusqu'à une autorisation écrite de Vincent, avec le périmètre consigné dans `docs/progress.md`.

Après autorisation : cocher chaque item, renseigner des **références génériques** (ex. « cluster lab-a », jamais un FQDN client), et garder les secrets hors dépôt (`.lab/` ignoré, coffre local).

## 0. Autorisation

- [ ] Autorisation écrite de Vincent (date, périmètre, durée)
- [ ] Entrée dans `docs/progress.md` : périmètre, niveau de preuve attendu, interdiction hors labo
- [ ] Aucune ressource de production dans le périmètre

## 1. Versions

- [ ] Version vCenter / build
- [ ] Version(s) ESXi du cluster de labo
- [ ] Surface API / REST utilisée (et écarts connus)
- [ ] Version du provider Terraform `vsphere` (lockfile catalogue)
- [ ] Attentes VMware Tools / cloud-init sur l'image Ubuntu

## 2. Comptes (identités séparées)

- [ ] Compte **inventaire** (lecture seule) : rôle et privilèges minimaux documentés
- [ ] Compte **mutation** (Terraform) : privilèges minimaux distincts (menace K3)
- [ ] Secrets scellés via le coffre Rackloom (jamais dans l'UI, le chat, les logs, le dépôt)
- [ ] Rotation de test documentée (F43)

## 3. TLS / CA

- [ ] Export de la CA du vCenter (PEM d'autorité uniquement)
- [ ] Import dans le connecteur (`ca_pem`) ; test de connexion T19 vert
- [ ] Nom d'hôte du certificat aligné avec l'URL `https://…` du connecteur
- [ ] Aucune option « ignorer TLS » (invariante 25)

## 4. Réseau

- [ ] VLAN / segment de management accessible aux runners Terraform (endpoints approuvés)
- [ ] Réseaux invités octroyés aux projets de labo (`resource_grants`)
- [ ] Filtrage sortant hôte (nftables) limité aux endpoints approuvés (menace K2)
- [ ] Réseau de quarantaine de labo si F40 doit être rejoué pour de vrai

## 5. Compute / stockage

- [ ] Cluster(s) et resource pool(s) de labo (`stable_id` d'inventaire)
- [ ] Datastore(s) de labo avec marge documentée
- [ ] Dossier / tags utilisés par le résolveur et le placement (F32)

## 6. Template Ubuntu

- [ ] Template Ubuntu 24.04 LTS (nom / `stable_id` d'inventaire)
- [ ] Guest OS id, VMware Tools, cloud-init ou personnalisation invitée
- [ ] Ancre de clé d'hôte publiée après personnalisation (remplace l'import labo, G1)
- [ ] Correspondance avec l'image de catalogue (ex. `img-ubuntu-2404-std` en fixture)

## 7. Backend de state (D4)

- [ ] Endpoint objet compatible S3 (ou forge) de **labo**, chiffrement, TLS
- [ ] Verrouillage natif ; préfixe par stack ; identifiants limités au préfixe
- [ ] Aucun state dans PostgreSQL applicatif (invariante 23)

## 8. Preuves et honnêteté

- [ ] Commandes réellement exécutées listées dans `docs/progress.md`
- [ ] Niveau de preuve porté à « validé en laboratoire » **uniquement** pour les capacités exercées sur ce labo
- [ ] Échec Ansible après création VM rejoué sur vraie VM (conservation, reprise ciblée)
- [ ] Seconde exécution sans doublon rejouée (plan `no_changes` / apply idempotent)
- [ ] Canari / secrets absents des logs et artefacts (T24)

## Références

- Contrainte D2 : `docs/progress.md`, `docs/project-context` (magasin projet)
- Plan Lot 2-D2 : `docs/lot-2-plan.md`
- Architecture lot 2 : `docs/architecture.md`
- Menaces K2–K4, G1 : `docs/threat-model.md`
- State distant : `docs/adr/0002-state-terraform-et-plans.md`
