# Plan du lot 2 (sous D2) : parcours vCenter/Terraform/Ansible testable localement

## 1. Contexte et contrainte

Le brief produit définit le lot 2 comme l'intégration vCenter / Terraform / Ansible sur un **laboratoire autorisé** : VM issue d'un template, logiciel de catalogue, service, paquets, logs ; seconde exécution sans doublon ; échec Ansible après création de VM (sans destroy).

**D2 (Vincent, 2026-11-25, toujours en vigueur)** : pas de laboratoire réel pour l'instant ; VM locales jetables et observations fictives explicites. Aucun vCenter, aucun identifiant réel, aucun déploiement externe.

Ce plan livre donc le **parcours Lot 2-D2** : le chemin d'acceptation du lot 2, exercé entièrement en local, avec inventaire / personnalisation invitée / attribution d'adresse **simulés** (badge « Simulé »). Le connecteur vSphere réel, le module Terraform `vsphere`, le state distant et le filtrage nftables restent **hors périmètre** jusqu'à levée de D2 (voir `docs/lab-vsphere-checklist.md`).

Niveau de preuve visé : **testé localement**. Jamais « validé en laboratoire réel ».

## 2. Objectifs d'acceptation (Lot 2-D2)

| Critère mission | Exercice local | Preuve |
|---|---|---|
| VM depuis template | Module Terraform `lab-vm` (provider `local`) + enrôlement `guest_ready` vers `lab-node` | `test_application_cree_exactement_les_vm` ; D1 |
| Logiciel de catalogue | Rôles `rackloom.nginx` / `rackloom.fail2ban` sur nœuds de labo | D1 ; AT01 |
| Service | `service_status = ready` + contrôles de santé | D1 ; e2e §1.1 |
| Paquets | Fiche machine `packages` | D1 ; e2e |
| Logs | Observations `kind=journal` (ACL T10) | D1 |
| Seconde exécution sans doublon | Replan → `no_changes` (T27) ; second passage Ansible `changed == 0` (AT01) ; un seul `tf_apply` après panne (T25) | `test_t27_*` ; `test_at01_*_second_passage` ; `smoke-d1.py` |
| Échec Ansible après création VM | T22 / CA-10 : `created_not_ready`, ressources conservées, reprise ciblée, aucun destroy | `test_t22_*` |
| Inventaire / suivi vCenter | `vsphere-fake` + badges « Simulé » | D1 ; fiche machine ; `PlanDetails` |

## 3. Hors périmètre (tranche laboratoire réel future)

Ne pas livrer dans Lot 2-D2 :

- session REST vCenter, inventaire et suivi réels ;
- module Terraform `vsphere` / provider HashiCorp ;
- backend de state distant (D4 ouverte) ;
- filtrage sortant nftables (menace K2) ;
- matrice de privilèges vCenter (K3) sur comptes réels ;
- toute mention « validé en laboratoire ».

Quand Vincent autorise un labo, suivre `docs/lab-vsphere-checklist.md` et consigner le périmètre dans `docs/progress.md` avant toute écriture externe.

## 4. Inventaire actuel (main)

| Couche | État sous D2 |
|---|---|
| Terraform CLI + `catalog/terraform/lab-vm` | réel, testé localement |
| Ansible Runner + rôles catalogue + `lab-node` | réel, testé localement |
| Connecteur `vsphere` | config + test TLS uniquement (T19) |
| Inventaire / guest / adresse / track | simulé (`vsphere-fake`, fixtures) |
| UI badges « Simulé » | livrés (plan, jobs, fiche machine, inventaire) |

## 5. Livrables de clôture

1. Renforcement T22 (conservation des ressources après échec Ansible) et T27 (seconde planification sans doublon).
2. `docs/lab-vsphere-checklist.md` : runbook futur (aucune adresse réelle).
3. Mise à jour `docs/progress.md` / `docs/backlog.md` / ce plan : Lot 2-D2 **terminé, testé localement** ; connecteur réel **non vérifié**.

## 6. Commandes de preuve

```text
cd backend && uv run pytest tests/integration/test_lab_flow.py::test_t22_echec_partiel_puis_reprise_ciblee \
  tests/integration/test_lab_flow.py::test_at01_vagues_nginx_fail2ban_et_second_passage \
  tests/integration/test_worker_resilience.py::test_t27_seconde_planification_sans_changement \
  tests/integration/test_lab_flow.py::test_d1_jusqu_au_succes_hote_sain -q
```

`make test` et, si la pile le permet, le sous-ensemble e2e D1 restent les preuves globales.
