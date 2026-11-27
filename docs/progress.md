# Avancement de Rackloom

## État au 2026-11-25

- **Lot 0 (cadrage)** : fusionné dans `main` (PR #1).
- **Lot 1 (parcours Compose avec résolveur central et modèle template/composant)** : **terminé, testé localement**. Livré en trois PR : #2 et #3 fusionnées en avance, puis la PR de clôture #4 (`cursor/lot-1-cloture-e231`).
- **Lot A** : **F28** fusionné (PR #6) ; **F27** fusionné (PR #7) ; **F21** fusionné (PR #11). **AT01** fusionné (PR #5) ; **F60** fusionné (PR #8) ; **F57** fusionné (PR #9) ; **F55** fusionné (PR #10).
- **F31** fusionné (PR #12). **F37** fusionné (PR #13). **F46** fusionné (PR #14). **F47–F50** fusionné (PR #16). **F01** fusionné (PR #17). **F05** fusionné (PR #18). **F03** fusionné (PR #20). **F09** fusionné (PR #21).
- **F51 à F54 et F56** (simplifier la demande et le suivi) : PR #15 (`cursor/f51-f56-demande-suivi-a149`), migration Alembic `0012`.
- **F32–F35** : PR #22 (`cursor/f32-f35-placement-outputs-licenses-ci-5bfe`), migration Alembic `0013` (après #15 ; GRANT worker inclus).
- **Lot C (F10–F14)** : fusionné (PR #24), migration Alembic `0014`, bundle `builtin@8` (R100, R140).
- **F36–F40 (résilience)** : fusionné (PR #25), migration Alembic `0015`, bundle `builtin@9` (R115, R135).
- **F41–F45 (identités et audit)** : fusionné (PR #26), migration Alembic `0016` (SCIM, revue d'accès, rotation, copie d'audit, certificats TLS).
- **F58–F59 (agir sur le service livré)** : fusionné (PR #27), migration Alembic `0017`.
- **F02 + F04 (compositions multi-VM et graphe)** : fusionné (PR #31), migration Alembic `0018`.
- **F06 (baux temporaires)** : fusionné (PR #28), migration Alembic `0019`.
- **F07 (fabrique d'images)** : fusionné (PR #30), migration Alembic `0020`.
- **F08 (adoption de VM)** : fusionné (PR #29), migration Alembic **`0021`**.
- **F15 (certificats SSH courts)** : PR en cours (`cursor/f15-ssh-certs-fa51`), migration Alembic **`0022`** (`down_revision=0021`).
- **Aucun laboratoire réel** (D2 et D21, décidé) : ressources locales jetables uniquement. Aucune capacité n'est « validée en laboratoire ». Aucun déploiement externe n'a eu lieu ; aucun identifiant réel n'a été utilisé.
- **Prochain jalon** : voir la dernière section.

## Niveaux de preuve

| Niveau | Sens dans ce document |
|---|---|
| simulé | l'étape n'existe pas réellement ; elle est jouée par un composant fictif et porte le badge « Simulé » dans l'UI |
| testé localement | exercé par `make test` ou `make test-e2e` sur ressources locales jetables (conteneurs, provider Terraform `local`), avec un test nominal et un test de refus ou d'échec |
| validé en laboratoire | session sur un laboratoire réel autorisé, consignée ici ; **aucune à ce jour** |
| non vérifié | code présent sans test qui l'exerce dans les conditions réelles |

## Capacités

| Capacité | Statut | Niveau de preuve | Preuves |
|---|---|---|---|
| Authentification locale, sessions, CSRF, `Origin`, rôles par projet (dont publicateur de templates) | livré | testé localement | T08, T29, T39 ; `tests/security/test_api_security.py` |
| Besoin versionné, rejet des champs techniques, résolveur central, révisions avec provenance | livré | testé localement | T21, T31 à T34, T37, T38 ; D2 à D4 dans le navigateur |
| Registre de composants et templates versionnés, publication, dépréciation, retrait (sans atelier) | livré | testé localement | T35, T39 ; D5 dans le navigateur |
| Parcours en trois étapes, formulaire généré, « Détails du plan » en lecture seule | livré | testé localement | T30 à T32 ; Playwright §1.1 au clavier, axe sans violation sérieuse ou critique |
| Machine à états, événements, audit chaîné | livré | testé localement | T03 à T08 ; chaîne d'audit vérifiée dans le parcours §1.1 |
| File PostgreSQL, bail, heartbeat, réconciliation | livré | testé localement | T13 à T15, T23, T25 |
| Point d'admission unique (dont règle 55, connecteur simulé refusé pour la production) | livré | testé localement | T07, T35, T40 |
| Launcher et runners isolés (sans réseau, socket du moteur seulement dans le launcher) | livré | testé localement | T28 ; runners réels dans `make test-e2e` |
| Plan et application Terraform (provider `local`, une stack par déploiement) | livré | testé localement | T01 à T05, T26, T27 |
| Approbation liée à la révision et à l'empreinte du plan | livré | testé localement | T03 à T08 |
| Ansible sur nœuds de labo, vérification, `service_status` distinct du succès des jobs, fiches machine | livré | testé localement (les « VM » sont des conteneurs `lab-node`) | T20, T22, T41 ; D1 |
| SSE et chronologie | livré | testé localement | T12 ; suivi en direct du parcours §1.1 |
| Secrets scellés (lecture du chiffré interdite au rôle API), rapports Ansible chiffrés en classe B | livré | testé localement | `test_db_roles.py`, T11, T24 |
| Assistant, adaptateur fictif déterministe | livré | simulé (badge « Assistant fictif ») ; refus testés localement | T06, T16 à T18, T36 |
| Assistant, adaptateur Ollama | code livré | non vérifié contre un vrai modèle (réponse HTTP simulée dans le test) | `test_chat.py` |
| Connecteur vSphere : configuration stricte et test de connexion TLS | livré | testé localement (serveur TLS jetable sur 127.0.0.1, CA générée) | T19 |
| Connecteur vSphere : inventaire, personnalisation invitée, attribution d'adresse, suivi | simulé (`vsphere-fake`) | simulé | badge « Simulé » sur chaque étape |
| Profil `production` : refus des connecteurs simulés, des identités de démo et de l'adaptateur fictif | livré | testé localement | `test_production_profile.py` |
| Exposition (seul `127.0.0.1:8443` publié) | livré | testé localement | T28 |
| Connecteur vCenter réel (session REST, inventaire) | lot 2 | non vérifié | — |
| ISO, premier boot, sauvegarde, restauration | lot 3 | non vérifié | — |
| OIDC | lot 4 | non vérifié | — |
| Gel d'urgence par environnement (F28, règle R080, bundle `builtin@2`) | livré | testé localement | `test_freeze.py`, `test_lab_flow.py` (F28), `60-f28-gel.spec.ts` ; vidéo `media/lot-a-f28-gel-urgence.webm` |
| Atelier de templates, première tranche (AT01) : brouillon assisté, revue, publication par une autre personne, vagues `pilot` | livré | testé localement | `tests/integration/test_studio.py` ; `tests/integration/test_lab_flow.py` (deux nœuds) |
| Fail2ban 1.0.0 : paquets, processus, jail `sshd` démarrée dans le journal | livré | testé localement | même parcours labo ; `verification_level` = `tested_locally` |
| Bannissement Fail2ban (nftables) | non couvert | non vérifié | les nœuds de labo n'ont pas NET_ADMIN ; le contrat le dit |
| Assistant de conception, adaptateur Ollama | code livré | non vérifié contre un vrai modèle | l'atelier est exercé avec l'adaptateur fictif |
| Template depuis un serveur de référence (F60) | livré (observations fictives explicites) | testé localement | `tests/integration/test_f60_reference.py` ; `web/e2e/96-atelier-f60.spec.ts` ; collecteur réel indisponible (D21) |
| Fiche « Votre service est prêt » (F57) | livré | testé localement | `tests/integration/test_f57_service_card.py` ; `web/e2e/97-atelier-f57.spec.ts` ; T41 pour le calcul réel |
| Boîte « À moi d'agir » (F55) | livré | testé localement | `tests/integration/test_f55_inbox.py` ; `web/e2e/98-atelier-f55.spec.ts` |
| Calendriers de changement (F27, règle R090, bundle `builtin@3`, migration `0004`) | livré | testé localement | `test_calendars.py`, `70-f27-calendriers.spec.ts` ; vidéo `media/lot-a-f27-calendriers.webm` |
| Chronologie de changement unifiée (F21) | livré | testé localement | `test_f21_timeline.py` ; `80-f21-chronologie.spec.ts` ; sources ADR ; scrub TB6 |
| Préflight de bout en bout (F31) | livré | testé localement | `test_f31_preflight.py` ; job `preflight` avant `tf_plan` ; TLS jetable → `tls_untrusted` ; state HTTP fermé → `state_unreachable` ; canari absent ; state inchangé ; L2 (vCenter/state réels) non fait |
| Disjoncteur par connecteur (F37, règle R095, bundle `builtin@4`) | livré | testé localement | `test_f37_circuit.py` ; ouverture → `connector_circuit_open` sur `write` ; `read` admis ; fermeture bloquée si `uncertain` ; L2 (coupure vCenter réelle) non fait |
| Banc d'évaluation Ollama (F46) | livré | testé localement | `test_f46_eval.py` ; e2e `87-f46-eval.spec.ts` ; `defaults_prod` / `asks_vlan` → échec ; rapport versionné (`model` + `prompt_id`) ; aucun job ; Ollama réel non qualifié (D2) |
| Chat F47–F50 (runbooks, routage, aperçu, préférences) | livré | testé localement | `test_f47_f50_chat.py` ; e2e `88-f47-f50-chat.spec.ts` ; migration `0008` ; fake Ollama OK ; aucun secret réel ; Ollama réel non (D2) |
| Catalogue Git avec promotion (F01) | livré | testé localement | `test_f01_catalogue_git.py` ; migration `0009` ; pins + promotions ; e2e `91-f01-catalogue.spec.ts` ; forge réelle : non |
| Contrôle de capacité (F05, règle R110, bundle `builtin@5`) | livré | testé localement | `test_f05_capacity.py` ; chute après approbation → `capacity_below_threshold` ; mesure vieillie → `capacity_stale` ; UI mesure sans réservation ; L2 non fait |
| Réservations IPAM/DNS (F03, règle R120, bundle `builtin@6`) | livré | testé localement | `test_f03_ipam.py` ; preview sans écriture ; concurrence ; `held_uncertain` ; migration `0010` ; IPAM réel : non |
| Détection de dérive (F09) | livré | testé localement | `test_f09_drift.py` ; hors Rackloom → dérive datée ; inventaire en erreur → observation_unavailable ; migration `0011` ; aucune correction ; L2 non fait |
| Exemples « qu'est-ce que je peux demander » (F51) | livré | testé localement | `test_f51_examples.py` ; e2e `91-f51-exemples.spec.ts` ; sans Ollama |
| Comparateur de choix métier (F52) | livré | testé localement | `test_f52_compare.py` ; e2e `92-f52-comparateur.spec.ts` ; coût/capacité omis (F05/F29/F32) |
| Demandes similaires (F53) | livré | testé localement | `test_f53_similar.py` ; e2e `93-f53-similaires.spec.ts` ; U1 |
| Passer la main / revues (F54) | livré | testé localement | `test_f54_reviews.py` ; e2e `94-f54-passer-la-main.spec.ts` ; migration `0012` |
| Estimation de mise à disposition (F56) | livré (file F36 branchée) | testé localement | `test_f56_estimate.py` ; e2e `99-f56-estimation.spec.ts` ; aucun modèle |
| File d'exécution équitable (F36) | livré | testé localement | `test_f36_fair_queue.py` ; quotas projet ; `wait_reason` ; estimate `queue` |
| Matrice de compatibilité (F38, R135, `builtin@9`) | livré | testé localement | `test_f38_compatibility.py` ; incompatible → blocage ; absent → non vérifié |
| Vérification sauvegardes VM (F39, R115) | livré (source fictive) | testé localement | `test_f39_backups.py` ; snapshot ≠ preuve ; labo réel non (D2) |
| Restauration VM réseau isolé (F40) | livré (simulé) | testé localement | `test_f40_restore.py` ; quarantaine ; nom distinct ; labo réel non (D2) |
| Placement multi-cluster et anti-affinité (F32) | livré | testé localement | `test_f32_placement.py` ; deux clusters → réplicas séparés, digest stable ; un cluster → `anti_affinity_unsatisfiable` ; cluster refusé dans le besoin ; L2 non fait |
| Sorties de déploiement réutilisables (F33) | livré | testé localement | `test_f33_outputs.py` ; `GET /deployments/{id}/outputs` ; allowlist ; hors projet → 404 ; jamais le state |
| Contrôle des licences (F34, règle R125, bundle `builtin@7`) | livré | testé localement | `test_f34_licenses.py` ; source fictive ; `license_unavailable` / `license_unverified` ; pas de clé dans le besoin ; source réelle non faite (D11) |
| Demandes depuis la CI/CD (F35) | livré | testé localement | `test_f35_ci.py` ; Bearer + `POST /ci/submissions` ; rejeu idempotent ; `locked_field` ; approbation refusée |
| Politiques versionnées et explicables (F10, R140, `builtin@8`) | livré | testé localement | `test_lot_c_f10_f14.py` ; exceptions propriétaire/périmètre/expiration ; simulation sans écriture ; refus avec `rule_id` + `field` |
| Droits temporaires (F11, R100 / R020) | livré | testé localement | grant → expiration → `temporary_right_expired` ; audit create/revoke |
| Réauthentification forte (F12) | livré | testé localement (IdP simulé) | step-up local + preuve IdP de test ; lecture OK sans preuve ; approbation refusée sans step-up ; IdP réel : non |
| Administration déléguée (F13) | livré | testé localement | rôles `project_admin` / `connector_admin` ; secrets jamais lus ; artefact hors projet → 404 |
| Accès d'urgence (F14) | livré | testé localement | `rackloom emergency` ; panne SSO simulée ; fermeture → `emergency_closed` ; `docs/emergency-access.md` |
| Arrivées et départs SCIM (F41) | livré (annuaire de test) | testé localement | `test_f41_f45_identity_audit.py` ; désactivation → cookie 401, jobs en file `blocked` ; jobs déjà envoyés inchangés ; IdP réel : non |
| Revue périodique des accès (F42) | livré | testé localement | campagnes + items ; périmètre project_admin → 404 hors projet ; révocation auditée |
| Rotation identifiants connecteurs (F43) | livré (test simulé) | testé localement | candidat invalide → pas de bascule, canari absent ; retire après fin des jobs |
| Copie d'audit hors appliance (F44) | livré (stockage local verrouillé) | testé localement | reprise sans doublon ; écrasement refusé si object_lock ; canari masqué |
| Registre certificats TLS (F45) | livré | testé localement | scan endpoint approuvé ; alerte `expiring_soon` au propriétaire ; TLS toujours vérifié |
| Actions courantes depuis la fiche service (F58) | livré (redémarrage simulé) | testé localement | `test_f58_f59_service_actions.py` ; e2e `101-f58-f59-actions-acces.spec.ts` ; migration `0017` ; remplacement annoncé et plan bloqué ; L2 non fait |
| Adoption de VM existantes (F08) | livré (observations / import simulés) | testé localement | `test_f08_adoption.py` ; e2e `102-f08-adoption.spec.ts` ; migration `0021` ; aperçu sans mutation ; `ownership_conflict` ; L2 non fait |
| Demande d'accès à un service (F59) | livré (faux IdP) | testé localement | correspondances approuvées ; `group` → 422 ; sync échouée visible ; relance idempotente ; IdP réel : non (D22) |
| Compositions multi-VM (F02) | livré | testé localement | `test_f02_composition.py`, `test_f02_f04_light.py`, `test_f02_f04_compositions_graph.py` (lab) ; template `ubuntu-app-base` ; migration `0018` ; L2 non fait |
| Graphe de dépendances (F04) | livré | testé localement | impact réseau filtré ; hors périmètre masqué/404 ; page `/dependances` ; e2e `102-f02-f04` ; L2 non fait |
| Baux pour environnements temporaires (F06) | livré (retrait simulé) | testé localement | `test_f06_leases.py` ; e2e `102-f06-baux.spec.ts` ; migration `0019` ; expiration → signalement + plan `retire` ; renew ; job `lease_retire` sans destroy ; L2 non fait |
| Fabrique d'images (F07) | livré (fabrique fictive) | testé localement | `test_f07_fabrique.py` ; e2e `102-f07-fabrique.spec.ts` ; migration `0020` ; outil réel (D11) / labo : non |
| Certificats SSH à courte durée (F15) | livré | testé localement | `test_f15_ssh_certs.py` ; `test_f15_ssh_lab.py` (docker) ; CA entreprise : non |
| Fonctions F16–F20, F22–F26, F29–F30 et atelier AT02 à AT04 restantes | à faire | non vérifié | `docs/backlog.md` |

## Démonstrations D1 à D6

Toutes passent dans Chromium (Playwright 1.65.1, TLS vérifié par une base NSS dédiée), au clavier, avec contrôle axe. Niveau : **testé localement**.

| Démo | Fichier | Ce qui est vérifié |
|---|---|---|
| D1 (§1.1) | `web/e2e/10-parcours-principal.spec.ts` | phrase D1 au chat, une seule question (date de fin), plan prêt, approbation par Bruno sur la même empreinte, succès « service prêt », badges « Simulé », modification (quantité 2), audit |
| D2 | `web/e2e/20-d2-verrouillage.spec.ts` | « Détails du plan » sans contrôle éditable ; réseau, vCPU et image refusés (422 `locked_field`) via l'API (besoin, entrées, à côté du besoin), le client CI et le chat ; version et empreinte inchangées |
| D3 | `web/e2e/30-d3-compta.spec.ts` | Chloé dans Compta : blocage `no_authorized_network` expliqué avec le référent, aucune question technique, aucun autre environnement essayé |
| D4 | `web/e2e/40-d4-environnement.spec.ts` | `dev` par défaut ; octroi réseau retiré → blocage, jamais `prod` ; `prod` sans confirmation → 422 ; scénario `defaults_prod` rejeté ; production confirmée → bandeau et circuit d'approbation de production |
| D5 | `web/e2e/90-d5-versions.spec.ts` | plan A approuvé en 1.0.0, 1.1.0 publiée avant le démarrage → A s'exécute en 1.0.0 (empreinte inchangée, déploiement épinglé) ; nouvelle demande → 1.1.0 ; 1.0.0 retirée avant le démarrage de B → job bloqué `resolution_no_longer_admissible`, plan intact |
| D6 | `web/e2e/50-d6-assistant.spec.ts` | assistant désactivé par l'administrateur, puis en panne : bandeau clair, parcours formulaire jusqu'à « Succès — service prêt » avec la même approbation, mêmes refus (API, CI, production non confirmée) |
| F28 | `web/e2e/60-f28-gel.spec.ts` | plan approuvé en file (worker arrêté), gel UI avec motif, redémarrage → `blocked` / `env_frozen` sans envoi ; inventaire et assistant (diagnostic) disponibles ; 403/404 ; dégel |
| F21 | `web/e2e/80-f21-chronologie.spec.ts` | sources ADR visibles ; coupure navigateur puis reprise sans doublon ; flux autre projet → 404 ; aucune sortie brute ; approbation + Terraform/vCenter/Ansible corrélés |

Capture du parcours principal et de F28 : `media/` du magasin du projet (voir le rapport de lot) ; elles ne sont pas versionnées.

## Tests obligatoires T01 à T41

Tous existent et passent ; aucun n'est marqué `skip` ou `xfail`. Niveau : **testé localement**.

| Tests | Où |
|---|---|
| T01, T03, T04, T05, T07, T08, T26, T35 (registre), T40 | `backend/tests/integration/test_terraform_flow.py` (vraie CLI Terraform) |
| T02, T13, T14, T15, T23, T27 | `backend/tests/integration/test_worker_resilience.py` |
| T05, T08, T32, T39 | `backend/tests/integration/test_api_flow.py` |
| T06, T16, T17, T18, T32, T34, T36 | `backend/tests/integration/test_chat.py` |
| T06, T09, T11, T12, T29, T32 | `backend/tests/security/test_api_security.py` |
| T10, T20, T22, T41, F28 (configuration bloquée puis reprise) | `backend/tests/integration/test_lab_flow.py` (Ansible réel contre des conteneurs `lab-node`) |
| F28 (admission, dispatch, concurrence, droits, audit) | `backend/tests/integration/test_freeze.py` |
| T19 | `backend/tests/security/test_connector_tls.py` |
| T21, T31, T32, T33, T34, T37, T38, T39 | `backend/tests/integration/test_resolver.py` |
| T24, T25, T28 | `scripts/test-e2e.sh` (`canary-scan.sh`, `smoke-d1.py --restart-during`, `check-exposure.sh`) |
| T30 à T36 | `web/e2e/*.spec.ts` |

## Commandes exécutées et résultats

Poste de développement : Linux x86_64, 4 vCPU, 15 Go, Docker 29.10.1, Compose 5.8.0, cgroup v2. Le 2026-11-25, sur la branche de clôture.

| Commande | Résultat |
|---|---|
| `make doctor` | OK, 1 avertissement (port 8443 occupé par la démo en cours) |
| `make test` : `ruff check`, `lint-imports`, `pytest`, `nuxi typecheck`, `check-openapi-drift.sh` | vert : `ruff` sans erreur ; 3 contrats import-linter tenus ; **179 tests passés** en 2 min 56 s (aucun `skip` ni `xfail`) ; `nuxi typecheck` sans erreur ; contrat OpenAPI et client typé à jour |
| `make typecheck` (mypy, non bloquant) | 136 erreurs (dette, voir les limites) |
| `make test-e2e` depuis une pile vide (6 nœuds de labo) | vert, terminé à 12:39 (heure de Paris) : T25 OK (appliance arrêtée pendant `ansible_configure`, changement réussi, service prêt, un seul `tf_apply`, aucun hôte en double) ; Playwright **6 tests passés** (§1.1 et D2 à D6) en 2,5 min ; T24 OK : canari absent des journaux Compose (339 Ko), de la base (856 Ko), des volumes, de 5 artefacts de classe B déchiffrés, de 376 réponses API et SSE et de 6 traces Playwright (172 Mo), témoin présent dans le journal du nœud ; T28 OK |
| CI GitHub Actions (`.github/workflows/ci.yml`) | vert sur un exécuteur GitHub vierge (Docker 28.0.4, Compose 2.38.2) : job `make test` (177 tests au premier passage) et job `make doctor` puis `make test-e2e` (6 min 20 s, mêmes étapes et mêmes résultats qu'en local) ; résultat sur le dernier commit dans la PR de clôture |

Détail de `make test-e2e` : la pile est détruite (`down -v`) puis reconstruite par `make demo` ; un canari aléatoire est posé comme mot de passe du connecteur `vsphere-fake` et injecté dans le journal nginx d'un nœud ; T25 coupe l'appliance pendant la configuration Ansible ; Playwright joue §1.1 et D2 à D6 avec traces complètes ; T24 cherche le canari dans les journaux Compose, toutes les tables, les volumes bruts, les artefacts de classe B déchiffrés, toutes les réponses API et SSE des identités de démo et les traces Playwright ; T28 contrôle l'exposition.

## Fichiers du lot 1

- `backend/` : API FastAPI, worker, résolveur, admission, registre, connecteurs, chat, CLI, migrations SQL ; tests unitaires, d'intégration, de sécurité.
- `launcher/` : launcher (moteur Docker en démo, processus local en test).
- `runners/` : images Terraform et Ansible (sans réseau), script commun.
- `catalog/` : template `ubuntu-web` 1.0.0 et 1.1.0, composant `nginx`, module Terraform `lab-vm`, rôles et playbooks Ansible.
- `compose/` : `compose.yaml`, `compose.demo.yaml`, `compose.test.yaml`, Caddy (`tls internal`), PostgreSQL.
- `lab/` : profil de démonstration, inventaire simulé, image `lab-node`.
- `web/` : WebUI Nuxt (3 étapes, Détails du plan, suivi SSE, fiches machine, administration), parcours Playwright.
- `openapi/openapi.json`, `web/generated/schema.d.ts` : contrat et client typé, sans dérive.
- `scripts/` : `doctor.sh`, `demo.sh`, `build-images.sh`, `test-e2e.sh`, `canary-*.{sh,py}`, `smoke-d1.py`, `check-exposure.sh`, `check-openapi-drift.sh`.
- `Makefile`, `.github/workflows/ci.yml`, `docs/progress.md`, `docs/backlog.md`.

## Ce que les fusions anticipées avaient laissé cassé dans `main`

Constaté sur `main` après la fusion des PR #2 et #3, corrigé dans la PR de clôture :

1. **Page de connexion corrompue** : le prérendu Nitro écrivait `index.html` en concurrence (exploration des liens), l'image servait un HTML tronqué. Exploration désactivée, et `web/scripts/csp.mjs` refuse un HTML généré corrompu.
2. **`PUT /connectors/{id}/secrets/{name}` en erreur 500** : l'upsert lisait `EXCLUDED.ciphertext`, colonne illisible pour le rôle API. Corrigé, avec un test sous le rôle API.
3. **« Modifier ce déploiement » vide** : la vue d'un changement `modify` sans version n'avait pas de besoin de départ. Corrigé (T26 vérifie le pré-remplissage).
4. **Parcours Playwright fusionné en échec** (récapitulatif et empreinte attendus différents de §1.1). Corrigé.
5. **Pas de `Makefile`, `make doctor` en échec, pas de CI** : ajoutés.
6. **Contrat d'architecture violé** : le chat importait le résolveur directement ; il passe par `changes.need`, et trois contrats import-linter le vérifient.
7. **T19 absent** et **démarrage en profil `production` non contrôlé** (définition de terminé, point 5) : ajoutés.
8. Le titre du commit de fusion de la PR #3 porte encore « [EN COURS — NE PAS FUSIONNER] » ; `main` n'a pas été réécrit.

## Écarts au plan et limites connues

- **H4 infirmée** : systemd ne démarre pas dans un conteneur sans `--privileged`. Les nœuds de labo gèrent nginx et sshd par leurs scripts ; les contrôles systemd sont affichés « non collecté » (repli prévu par H4).
- **Clés privées SSH de labo** scellées en base (et non sur un volume du worker) ; seules les clés publiques sont sur le volume `lab_keys`.
- **Une seule pile à la fois** sur un poste : les noms `rackloom_*` des volumes et réseaux sont fixes. `make test-e2e` détruit la pile de démonstration.
- **Réseaux internes isolés** : `core`, `ai` et `rn-lab` sont `internal` ; l'API n'a aucune sortie réseau en démo, donc le test de connexion vSphere réel n'est exercé que par `pytest`.
- **Inventaire, personnalisation invitée, attribution d'adresse** : simulés (`vsphere-fake`) jusqu'au lot 2. Le connecteur vSphere réel n'a que la configuration et le test TLS ; sa session REST arrive au lot 2.
- **Ollama** : adaptateur codé, jamais exécuté contre un vrai modèle.
- **Fail2ban** (D15) : livré avec AT01. La jail `sshd` est observée dans le journal (testé localement). Le bannissement effectif n'est pas couvert : les nœuds de labo n'ont pas la capacité NET_ADMIN.
- **Double validation** (D16) : non implémentée. Le siège unique de publicateur est conservé. La proposition est l'ADR-0010, statut proposé.
- **Parcours navigateur AT01** : le scénario Playwright `web/e2e/95-atelier-at01.spec.ts` est écrit (après D5, base lue sur le serveur). Il n'a pas été exécuté sur la pile dans cette tranche ; la preuve d'exécution est le test d'intégration sur deux nœuds de labo.
- **Canari T24** : aucun composant du catalogue ne référence encore de secret, donc le chemin « secret transmis à un runner » n'est pas exercé ; le canari passe par le connecteur, les journaux de nœud et toutes les sorties.
- **mypy** : 136 erreurs restantes (447 au départ), surtout des annotations de lignes psycopg ; `make typecheck` est non bloquant au lot 1. `ruff format` n'est pas appliqué (seul `ruff check`).
- **Poste de développement** : sur la VM de travail, des tables `iptables-legacy` résiduelles bloquaient le trafic entre ponts Docker ; une règle ACCEPT entre ponts a été ajoutée sur l'hôte (hors dépôt). Les exécuteurs GitHub n'en ont pas besoin.
- **CI** : `make test-e2e` construit toutes les images à chaque exécution (pas de cache d'images) ; les sorties Playwright ne sont pas publiées comme artefacts, car elles contiennent les mots de passe de démo saisis.

## Décisions prises

| Date | Décision | Par |
|---|---|---|
| 2026-11-25 | Règle produit : l'utilisateur choisit le besoin, Rackloom choisit la technique ; résolveur central, trois catégories de données et parcours en trois étapes intégrés au lot 1 | Vincent |
| 2026-11-25 | Les profils de service sont des versions publiées et immuables de templates Rackloom ; le lot 1 livre le modèle template/composant, un registre minimal et un template publié de démonstration, sans atelier | Vincent |
| 2026-11-25 | F51 à F60 gardent la numérotation de Vincent ; l'atelier de templates est identifié AT01 à AT04, et F60 dépend de AT01 | Vincent (via le coordinateur) |
| 2026-11-25 | **Ordre de livraison** : lot 1 ; puis, en parallèle sur deux branches, l'atelier de templates (AT01) suivi de F60, F57 et F55 sur la même branche, et le lot A (F28, F27, F21) ; ensuite F31, F37, F46 ; puis le reste de F01 à F50 et F51 à F54, F56, F58, F59 selon leurs dépendances. Lot 2 et lot 3 selon la disponibilité du laboratoire | Vincent |
| 2026-11-25 | Le scénario de référence du brief F51 à F60 est le critère d'acceptation de la tranche AT01, F60, F57, F55, pas du lot 1 | Vincent (via le coordinateur) |
| 2026-11-25 | D1, D2, D7, D14, D16, D19 et D21 tranchées conformément aux propositions (détail dans le tableau suivant) | Vincent (via le coordinateur) |

## Décisions à trancher par Vincent

Les décisions marquées « décidé » sont tranchées ; les autres restent ouvertes avec leur valeur proposée, appliquée provisoirement au lot 1.

| # | Décision | Proposition ou décision | Bloque | Statut |
|---|---|---|---|---|
| D1 | CI et registre d'images pour le dépôt GitHub `vincenttisseront/rackloom-` (public ; tiret final volontaire ?) | dépôt public conservé, nom `rackloom-` conservé, GitHub Actions et GHCR | E0 (pipeline) | décidé (Vincent, 2026-11-25) ; CI livrée, aucune image poussée vers GHCR au lot 1 |
| D2 | Périmètre et date d'un laboratoire réel autorisé (vCenter, version, comptes, réseau, image Ubuntu) | pas de laboratoire réel pour l'instant ; VM locales jetables et observations fictives explicites | lot 2, AT01 sur VM jetables, F60, toute mention « validé en laboratoire » | décidé (Vincent, 2026-11-25) |
| D3 | Redistribution de Terraform (BSL 1.1) et des providers dans l'ISO ; qualification éventuelle d'OpenTofu | avis juridique avant le lot 3 | lot 3 | ouverte |
| D4 | Backend de state distant réel | stockage objet compatible S3 interne avec verrouillage natif | lot 2 | ouverte |
| D5 | Moteur des runners sur l'appliance | Podman sans root (ADR-0003) | lot 3 | ouverte |
| D6 | Une reprise ciblée exige-t-elle une nouvelle approbation ? | oui (approbation de type `retry`) | E9 | ouverte (appliquée : T22) |
| D7 | L'administrateur de plateforme lit-il les données des projets sans rôle projet ? | l'administrateur de plateforme n'a pas accès aux données d'un projet sans rôle attribué et audité | E1 | décidé (Vincent, 2026-11-25) |
| D8 | Qui peut geler un environnement (F28) | administrateurs de plateforme et approbateurs de l'environnement | F28 | ouverte (appliquée provisoirement dans F28) |
| D9 | Calage du lot 2 (laboratoire réel) et du lot 3 (ISO) dans l'ordre décidé | lot 2 dès qu'un laboratoire existe, pour valider F21, F05, F03, F09 et AT01 ; lot 3 avant F17 et F26 ; OIDC avant F12, F14, F41 et F59 | planification | ouverte |
| D10 | IdP cible et second facteur local avant le lot 4 | à préciser | F12, F14, F41, F59, usage hors labo | ouverte |
| D11 | Outils tiers du backlog : IPAM (F03), ITSM (F25), CA SSH (F15), fabrication d'images (F07), sauvegarde (F39), licences (F34) | à préciser selon l'existant | ces fonctions | ouverte |
| D12 | Départage du résolveur entre candidats équivalents (H13) | rang dans la liste du template, puis identifiant stable ; aucune question tant qu'il n'y a qu'un site | E4, F32 | ouverte (appliquée : T37) |
| D13 | Environnement par défaut quand plusieurs environnements hors production sont admissibles (H14) | l'utilisateur choisit parmi eux ; jamais la production | E4 | ouverte (appliquée : T34, D4) |
| D14 | Date de fin exigée en `dev` (H16) | date de fin obligatoire en `dev`, 30 jours au plus ; seule question du scénario D1 | E2, E4 | décidé (Vincent, 2026-11-25) |
| D15 | Composant Fail2ban | livré avec AT01 : jail observée dans le journal ; bannissement non couvert sans NET_ADMIN | E10, AT01 | livré (testé localement, 2026-11-25) |
| D16 | Qui tient le rôle `template_publisher` ; double validation pour publier | une seule personne publie ; la double validation est proposée dans l'ADR-0010 et n'est pas implémentée | E3, AT01 | décidé pour le siège unique (Vincent, 2026-11-25) ; double validation encore à trancher |
| D17 | Référent affiché dans les messages de blocage | référent désigné par projet (`projects.referent_user_id`) | E2, E4 | ouverte (appliquée : T33, D3, D4) |
| D18 | Signature des versions publiées de templates et composants | à étudier avec AT01 ou F17 | AT01, F17 | ouverte |
| D19 | Succès d'un changement conditionné à un service prêt | « Succès » seulement si tous les contrôles de santé réussissent, sinon « Succès partiel » avec le motif (T41) | E10 | décidé (Vincent, 2026-11-25) |
| D20 | Priorités proposées dans le backlog : P1 pour F31, F37, F46, F60, F57, F55 et AT01 ; P2 ou P3 pour les autres nouvelles entrées | à confirmer | planification | ouverte |
| D21 | Serveur de référence et collecteurs réels pour F60 (VM, partitionnement, périmètre d'observation) | pas de laboratoire réel pour l'instant ; VM locales jetables et observations fictives explicites | F60 | décidé (Vincent, 2026-11-25) |
| D22 | Fournisseur d'identité en écriture pour F59 et annuaire pour F41 | à préciser | F41, F59 | ouverte |

## Journal des lots

### Lot 0 : cadrage (2026-11-25)

- Fichiers créés : `CLAUDE.md`, `docs/architecture.md`, `docs/product-spec.md`, `docs/threat-model.md`, `docs/progress.md`, `docs/backlog.md`, `docs/lot-1-plan.md`, `docs/adr/README.md`, `docs/adr/0001` à `0009`.
- Compléments intégrés le même jour :
  1. backlog F01 à F30 : point d'admission unique, identifiant de corrélation et politiques versionnées réservés dans l'architecture et le plan ;
  2. règle « l'utilisateur choisit le besoin, Rackloom choisit la technique » (brief F31 à F50) : résolveur central, trois catégories de données, rejet des champs techniques, révisions, parcours en trois étapes, démonstrations D1 à D6 comme critères du lot 1, ADR-0008, F31 à F50 au backlog ;
  3. templates Rackloom et registre de composants (brief de l'atelier) : modèle de données, registre minimal et template publié au lot 1, trois droits distincts, ADR-0009, atelier hors lot 1 (AT01 à AT04) ;
  4. fonctions utilisateur F51 à F60 : état du service distinct du succès des jobs (T41), actions calculées côté serveur, règles des collecteurs d'observation et des demandes d'accès, menaces correspondantes, F51 à F60 au backlog, ordre de livraison décidé par Vincent.
- Commandes exécutées : aucune commande de construction ni de test (dépôt vide).
- Résultat : architecture, modèle de menaces, ADR, profil de démonstration, plan du lot 1 et backlog rédigés.

### Lot 1 : parcours Compose (2026-11-25)

- PR #2 (parcours complet dans Compose) et PR #3 (T26, pile à six nœuds, parcours Playwright §1.1) fusionnées en avance ; PR de clôture depuis `cursor/lot-1-cloture-e231`.
- Commandes et résultats : section « Commandes exécutées et résultats ».
- Résultat : D1 à D6 et T01 à T41 passent, testés localement ; aucune capacité validée en laboratoire.

### Lot A : F28 gel d'urgence (2026-11-25, heure de Paris)

- Branche `cursor/lot-a-f28-gel-20f1`. Migration `0002` (`environment_freezes`), règle d'admission R080 (étapes `start` et `dispatch`), bundle `builtin@2`, API `/environments/{id}/freeze|unfreeze`, `/admin/environments`, bandeaux UI, D8 provisoire (admin plateforme ou approbateur).
- Commandes : `make test` → **193 tests passés** (dont 7 `test_freeze` + 1 labo F28) ; `RACKLOOM_E2E_VIDEO=1 ./scripts/test-e2e.sh 60-f28-gel` → Playwright F28 OK (~19 s de vidéo), T25/T24/T28 OK. E2e de référence sur `main` avant changement : 6 Playwright verts + T25/T24/T28 OK.
- Suivi CI : e2e a échoué une fois sur T25 (course : `ansible_configure` interrompu → `partially_succeeded` + réconciliation OK, données conservées, un seul `tf_apply`). Correctif : `finish_configure` (sorties runner, miroir de `finish_apply`) ; `smoke-d1.py` accepte reprise **ou** réconciliation sans doublon (lot-1-plan T25).
- Preuves : `backend/tests/integration/test_freeze.py`, `test_lab_flow.py::test_f28_…` et `test_t25_reconcile_ansible_depuis_sorties`, `web/e2e/60-f28-gel.spec.ts`, vidéo magasin `/cursor/stores/…/media/lot-a-f28-gel-urgence.webm`. Niveau : **testé localement**. Labo réel : non (D2).
- Limites : un job déjà `dispatched` n'est pas stoppé (suivi seulement) ; un job bloqué ne repart pas au dégel (nouvelle version ou reprise) ; D8 non tranchée formellement ; pas de job réellement envoyé à vCenter.
- Suite du lot A : **F27** (calendriers), puis **F21** (chronologie unifiée).

### AT01 : atelier, Fail2ban et vagues (2026-11-25)

- Fusionné dans `main` (PR #5). Migration `0003_atelier` (après F28 `0002`).
- Résultat : atelier et vagues testés localement. Aucune capacité validée en laboratoire. Double validation : ADR-0010 proposé.

### Lot A : F27 calendriers (2026-11-25, heure de Paris)

- Branche `cursor/lot-a-f27-calendriers-20f1` rebasée sur `main` (F28 + AT01 + F60 + F57). Migration **`0004`** (`down_revision = 0003_atelier`) — F60 et F57 n'ajoutent aucune migration Alembic. R090 (`start`/`dispatch`), `builtin@3`, tables `environment_calendars` / `change_windows` / `calendar_freezes`, horloge injectable `rackloom.clock`, programmation `run_after` à l'approbation (y compris vagues ansible AT01).
- Conflits post-F60 / post-F57 : seulement `docs/progress.md` (simples). Aucun conflit d'intention.
- E2e F27 : gel posé via API (ISO UTC) + `run_after` forcé en SQL avant redémarrage worker (évite course CI où le changement restait « Approuvée »).
- Preuves : `test_calendars.py` (7) ; Playwright `70-f27-calendriers.spec.ts` ; vidéo magasin `media/lot-a-f27-calendriers.webm`.
- Suite : **F21** après fusion de F27 (PR #7).

### F60 : serveur de référence (2026-11-25)

- Branche `cursor/atelier-f60-88f4` depuis `main` (AT01 fusionné, `2ff90b7`).
- Collecteurs approuvés (liste blanche) ; fixture `ubuntu-nginx-dedicated-mount` avec badge « Observations fictives » (D21).
- Trois catégories UI : À standardiser / Propre à cet hôte / À confirmer ; layout `/var/www` signalé ; canaris absents du brouillon ; aucun job d'écriture.
- Preuves : `uv run pytest tests/integration/test_f60_reference.py` — 7 verts. Collecteur réel et labo VM : non couverts (consignés).

### F57 : fiche service (2026-11-25)

- Branche `cursor/atelier-f57-88f4` depuis `main` (F60 fusionné).
- Fiche courte : quatre états, sorties `outputs_allowlist`, preuves et heure des contrôles ; détails techniques au second niveau.
- Preuves : `test_f57_service_card.py` (7) ; e2e `97-atelier-f57.spec.ts` (prêt réel + UI des autres états).

### F55 : boîte « À moi d'agir » (2026-11-25)

- Branche `cursor/atelier-f55-88f4` depuis `main` (F57 fusionné).
- `GET /inbox` : « À moi d'agir » / « En attente d'une autre personne » via `workflow.available_actions`.
- Preuves : `test_f55_inbox.py` (4) ; e2e `98-atelier-f55.spec.ts` (alice, bruno, david ; révocation → 403).

### Lot A : F21 chronologie (2026-11-25, heure de Paris)

- Branche `cursor/lot-a-f21-chronologie-20f1` depuis `main` (F27 fusionné) ; fusionnée (#11).
- Sources ADR (`request`, `approval`, `admission`, `job`, `terraform`, `ansible`, `vcenter`, `verification`, `chat`, `system`) ; scrub des sorties brutes (TB6) ; `approval.decided` ; reprise SSE / Last-Event-ID ; UI libellés de source.
- Preuves : `test_f21_timeline.py` (3) ; Playwright `80-f21-chronologie.spec.ts` ; vidéo magasin `media/lot-a-f21-chronologie.webm`. Labo réel vCenter : non (D2).

### F31 préflight (2026-11-26, heure de Paris)

- Branche `cursor/f31-preflight-9304` depuis `main` (F21 fusionné) ; fusionnée (#12).
- Job `preflight` (classe `read`) enfilé à la soumission / replan ; `tf_plan` seulement après succès ; contrôles vCenter (TLS / simulé), state (local_demo ou HTTP), prérequis catalogue ; motifs `tls_untrusted` / `state_unreachable` sans identifiant ni canari.
- Preuves : `backend/tests/integration/test_f31_preflight.py`. Labo réel : non (D2).

### F37 disjoncteur (2026-11-26, heure de Paris)

- Branche `cursor/f37-circuit-breaker-cd8f` depuis `main` (F31 fusionné) ; fusionnée (#13).
- Migration `0006` ; états `closed`/`open` sur `connectors` ; règle R095 (`start`/`dispatch`) ; bundle `builtin@4` ; seuil `connector_circuit_failure_threshold` ; fermeture après test de santé seulement s'il ne reste aucun job `uncertain` lié.
- Preuves : `test_f37_circuit.py`. Labo réel (coupure vCenter) : non (D2).

### F46 banc d'évaluation Ollama (2026-11-26, heure de Paris)

- Branche `cursor/f46-ollama-eval-bench-4c10` depuis `main` (F37 fusionné) ; fusionnée (#14).
- Migration `0007` (`chat_eval_reports`, ajout seul) ; prompt versionné `rackloom.chat-prompt/v1` ; `rackloom.chat.eval` rejoue des conversations fictives ; métriques (questions inutiles, résolution du besoin, ciblage, champs verrouillés) ; `defaults_prod` / `asks_vlan` → `verdict=failed` ; API admin + `rackloom chat eval` ; UI Connecteurs.
- Preuves : `test_f46_eval.py` ; e2e `87-f46-eval.spec.ts`. Qualifier un vrai modèle Ollama : non (D2).

### F47–F50 chat opérateur (2026-11-26, heure de Paris)

- Branche `cursor/f47-f50-chat-runbooks-routing-d40b` depuis `main` (F46 fusionné) ; fusionnée (#16).
- **F47** : `catalog/runbooks/` + table `runbooks` ; sélection serveur ; `diagnosis` (faits / hypothèses / remédiation `request_draft`) ; aucun job.
- **F48** : `chat.routing` ; modèle choisi côté serveur ; refus `/model` ; analyse indisponible → motif clair, formulaire intact, pas de cloud.
- **F49** : `POST …/context-preview` ; retrait d'extraits seulement ; canari filtré (aperçu, envoi, résumé).
- **F50** : `project_preference_versions` ; publication référent ; résolveur provenance `project_preference` ; plan existant inchangé ; chat → `preference_not_from_chat`.
- Preuves : `test_f47_f50_chat.py` ; e2e `88-f47-f50-chat.spec.ts`. Ollama réel : non (D2).

### F01 : catalogue Git avec promotion (2026-11-26, heure de Paris)

- Branche `cursor/f01-catalogue-git-235e` depuis `main` (F47–F50 fusionné) ; fusionnée (#17).
- Migration `0009` : `catalog_source_pins`, `template_promotions` ; backfill des versions déjà publiées.
- Import Git local épinglé (`rackloom catalog import-git`), revue (`review-pin` / API), publication refusée si non revue ; promotion explicite pour sources Git (filesystem : promotion initiale auto à la publication).
- Résolveur : nouvelles demandes → `latest_promoted` pour le kind d'environnement.
- Preuves : `test_f01_catalogue_git.py` (2) ; T35 / résolveur verts. Forge réelle : non (D2).

### F05 : contrôle de capacité (2026-11-26, heure de Paris)

- Branche `cursor/f05-capacite-235e` (sur F01) ; fusionnée (#18). Règle R110 (`start`/`dispatch`), bundle `builtin@5`.
- Mesures CPU/mémoire/stockage sur inventaire fictif ; âge ; jamais une réservation ; rejeu à l'admission.
- Preuves : `test_f05_capacity.py` (2). Cluster réel : non (D2).

### F03 : IPAM/DNS (2026-11-26, heure de Paris)

- Branche `cursor/f03-ipam-dns-235e` (sur F05) ; fusionnée (#20). Migration `0010` ; R120 ; `builtin@6` ; adaptateur `ipam-fake` (192.0.2.0/24).
- Cycle prepare → reserve → confirm / held_uncertain ; preview sans écriture.
- Preuves : `test_f03_ipam.py` (4). IPAM réel : non (D2, D11).

### F09 : détection de dérive (2026-11-26, heure de Paris)

- Branche `cursor/f09-derive-235e` (sur F03) ; fusionnée (#21). Migration `0011` ; job `drift_scan` (read) ; API `/deployments/{id}/drift`.
- Comparaison state labo vs instances ; panne inventaire → `observation_unavailable` ; aucune correction.
- Preuves : `test_f09_drift.py` (2). Labo vCenter réel : non (D2).

### F51–F54 et F56 : simplifier la demande et le suivi (2026-11-26, heure de Paris)

- Branche `cursor/f51-f56-demande-suivi-a149` ; rebase sur `main` (Lot B F01/F05/F03/F09, tip `0f1606b`) avant merge.
- **F51** : `GET /examples`, `GET /projects/{p}/examples` ; UI accueil + nouvelle demande ; `can_submit` ≠ `can_execute` ; aucun exemple prod sans droit.
- **F52** : `POST /projects/{p}/comparisons` ; services / environnements / niveaux ; critères non vérifiés omis ; mode `auto` si une seule option.
- **F53** : `POST /projects/{p}/changes/similar` ; dépôt scopé avant similarité (U1) ; aucune fusion.
- **F54** : migration `0012` (`change_reviews`, après `0011` F09) ; sollicitation / réponse ; events F21 ; aucun `role_binding`.
- **F56** : `GET /changes/{id}/estimate` ; approbation / créneau F27 / fourchette historique ; file F36 branchée (`idle`/`ready`/`waiting`) ; `model_used: false`.
- Preuves : `test_f51_examples.py` … `test_f56_estimate.py` (15) ; e2e `91`–`94`, `99` ; CI. Labo réel : non (D2).

### F58–F59 : agir sur le service livré (2026-11-27, heure de Paris)

- Branche `cursor/f58-f59-actions-acces-6117` depuis `main` (après F41–F45 / `0016`).
- Migration `0017` : kinds `restart` / `service_restart` ; tables `service_access_mappings`, `service_access_requests`, `service_access_assignments`, `idp_fake_state` ; GRANT api/worker.
- **F58** : `service_actions` sur le template ; `workflow.available_actions` + `POST /deployments/{id}/actions/{action}` ; capacité → modify prérempli ; redémarrage → plan simulé + job ; impact remplacement/interruption visible ; sans droit → 403/404.
- **F59** : niveaux `access_levels` ; correspondances publiées (démo) ; `group`/`idp_group` saisis → 422 ; « accès accordé » après faux IdP ; sync_failed visible ; relance idempotente.
- Preuves : `test_f58_f59_service_actions.py` ; e2e `101-f58-f59-actions-acces.spec.ts`. IdP / labo réels : non (D2, D22).

### F36–F40 résilience (2026-11-27, heure de Paris)

- Branche `cursor/f36-f40-resilience-fd86` depuis `main` (après Lot C / `0014`).
- Migration `0015` : `execution_quotas`, `compatibility_matrix`, `backup_proofs`, `jobs.wait_reason`, kinds `restore` / `vm_restore` ; GRANT api/worker.
- Bundle `builtin@9` : R115 (F39), R135 (F38).
- **F36** : claim saute les projets au quota ; estimate expose `queue` ; aucun choix de runner.
- **F38** : matrice `matrix@1` ; providers du lockfile ; incompatible → `compatibility_incompatible`.
- **F39** : source fictive ; snapshot vSphere → `backup_snapshot_not_proof` ; observations `simulated=true`.
- **F40** : `restore_profile` + réseau quarantaine ; `POST /deployments/{id}/restore` ; job `vm_restore` simulé.
- Preuves : `test_f36_fair_queue.py`, `test_f38_compatibility.py`, `test_f39_backups.py`, `test_f40_restore.py` ; e2e `96-f36-f40-resilience.spec.ts`. Labo réel / produit sauvegarde : non (D2).

### F32–F35 placement, sorties, licences, CI (2026-11-26, heure de Paris)

- Branche `cursor/f32-f35-placement-outputs-licenses-ci-5bfe` ; rebase sur #15 (`f25d7a5`, F54 `0012`).
- **F32** : inventaire démo à deux clusters/sites ; placement par réplica + datastore de même site ; `anti_affinity` hors de `placement` (bundle n'attend que des `stable_id`) ; activé seulement quand le profil l'exige ; blocage `anti_affinity_unsatisfiable` ; R055 vérifie tous les clusters/datastores.
- **F33** : table `deployment_outputs` ; publication à l'apply (allowlist) ; `GET /deployments/{id}/outputs` scopé ; jamais le state ; GRANT worker pour Compose.
- **F34** : module `licenses` (source fictive) ; règle R125 ; bundle `builtin@7` ; `license_unverified_policy=block`.
- **F35** : utilisateurs `kind=service` + `service_tokens` ; Bearer sans CSRF ; `POST /ci/submissions` idempotent ; approbation refusée (`service_identity_forbidden`).
- Migration `0013` (après F54 `0012`). Preuves : `test_f32_placement.py`, `test_f33_outputs.py`, `test_f34_licenses.py`, `test_f35_ci.py`. Labo réel / source licences réelle : non (D2, D11).

### F41–F45 identités et audit (2026-11-27, heure de Paris)

- Branche `cursor/f41-f45-identites-audit-7aae` depuis `main` (après F36–F40 / `0015`).
- Migration `0016` : `scim_tokens`, `scim_sync_events`, `users.external_id`, campagnes de revue, rotations de secrets, destinations/livraisons d'export d'audit, `tls_certificate_records` ; GRANT `rackloom_api` / `rackloom_worker`.
- **F41** : SCIM 2.0 Users (Bearer) + sync fixture ; désactivation → `privilege_epoch`, jobs `queued` → `blocked` (`identity_disabled`) ; R020 explicite.
- **F42** : préparation automatique (orphelins, bindings injustifiés, exceptions/grants expirés) ; décision confirmée/révoquée auditée ; hors périmètre → 404.
- **F43** : propose / validate / cutover / retire sans reveal API ; sondage simulé à la proposition ; valeur jamais dans UI/logs.
- **F44** : stockage local à verrouillage d'objets ; `dedupe_key` = hash d'audit ; reprise sans doublon.
- **F45** : scan TLS des endpoints approuvés ; alerte d'expiration ; aucune clé privée.
- Preuves : `test_f41_f45_identity_audit.py` ; e2e `100-f41-f45-identites.spec.ts` ; page `/admin/identites`. Annuaire / stockage / vCenter réels : non (D2).
- Correctif UX : hub `/admin` (`pages/admin/index.vue`) + lien nav « Administration » — avant, `/admin` tombait en 404 Nuxt (main et branche) alors que Vincent signalait la section indisponible.
- Correctif e2e F44 : volume Compose `audit-export` monté en écriture sur l’API (conteneur `read_only`) — sans lui, `POST /admin/audit-exports` échouait en silence côté UI.

### F02 + F04 : compositions et graphe (2026-11-27, heure de Paris)

- Branche `cursor/f02-f04-compositions-graphe-586c` depuis `main` (après F58–F59 / `0017`).
- Migration `0018` : `hosts.composition_member` ; `dependency_graph_builds`, `dependency_nodes`, `dependency_edges` ; GRANT api/worker.
- **F02** : bloc `composition.members` (dépendances + readiness) ; vagues topologiques ; composants Ansible par membre ; template `ubuntu-app-base` (socle Fail2ban → application Nginx) ; échec partiel → vague suivante bloquée, aucune suppression ; reprise ciblée existante.
- **F04** : reconstruction lecture seule ; provenance confirmée/inférée/inconnue ; `GET /dependency-graph`, impact filtré (404 hors périmètre) ; UI `/dependances`.
- Preuves locales : unitaires composition ; intégration légère (résolution + impact scopé) ; suite non-terraform verte. Recette labo terraform/ansible : CI. Labo réel : non (D2).

### F06 baux temporaires (2026-11-27, heure de Paris)

- Branche `cursor/f06-baux-temporaires-91eb` rebasée sur `main` après F02/F04. Migration **`0019`** (`down_revision=0018`).
- Tables `deployment_leases`, `deployment_lease_renewals` ; kinds `retire` / `lease_retire` ; GRANT api/worker.
- Ouverture du bail à la soumission si `lifetime_until` ; sweep worker (rappel, expiration, proposition de plan) ; renouvellement (plafond D14) ; plan de retrait approuvable ; exécution simulée (pas de destroy Terraform).
- Preuves : `test_f06_leases.py` ; e2e `102-f06-baux.spec.ts` ; fiche service (`lease-panel`).

### F07 : fabrique d'images (2026-11-27, heure de Paris)

- Branche `cursor/f07-fabrique-templates-d8de` rebasée sur `main` après F06. Migration **`0020`** (`down_revision=0019`).
- Tables `image_builds`, `image_build_tests`, `image_build_promotions` ; GRANT `rackloom_api` / `rackloom_worker`.
- Module `factory` + constructeur fictif (`builder=fake`, D11) : draft → build → tests → ready/failed → promote / deprecate.
- Recette : `failed` → `409 image_tests_failed` (y compris prod) ; `ready` → promotion OK ; dépréciation sans retrait.
- UI `/admin/images` ; seed démo `ubuntu-2404-std@1.0.0` aligné sur le filtre ubuntu-web.
- Preuves : `test_f07_fabrique.py` (2) ; e2e `102-f07-fabrique.spec.ts` ; `test_db_roles`. Outil Packer/vSphere réel : non.

### F08 : adoption de VM existantes (Lot D, 2026-11-27, heure de Paris)

- Branche `cursor/f08-adoption-vm-6c45` rebasée sur `main` après F07. Migration Alembic **`0021`** (`down_revision=0020`). Tables `vm_adoptions`, `adoption_observations`, `terraform_resource_claims` ; kind `tf_import` ; GRANT `rackloom_api` / `rackloom_worker` ; octroi inventaire `vm`.
- Enrôlement + observation **fictive** lecture seule ; aperçu d'import sans mutation VM/state ; proposition séparée de l'exécution ; approbation liée à l'empreinte ; conflit de propriété → `ownership_conflict` (R145).
- Inventaire démo : `vm-9001` (orphan) / `vm-9002` (state externe fictif). UI `/projets/{slug}/adoption`.
- Preuves : `test_f08_adoption.py` (3) ; e2e `102-f08-adoption.spec.ts`. Labo vSphere réel : non (D2).

### F15 : certificats SSH à courte durée (Lot E, 2026-11-27, heure de Paris)

- Branche `cursor/f15-ssh-certs-fa51` depuis `main` (après F08 / `0021`). Migration Alembic **`0022`** (`down_revision=0021`). Tables `ssh_cas`, `ssh_certificate_issues` ; séquence `ssh_certificate_serial` ; GRANT SELECT/INSERT/UPDATE/DELETE `rackloom_api` / `rackloom_worker`.
- Module `sshca` : CA ed25519 scellée (`secret://lab/ssh-ca/signing`) ; émission par job (principals `identité@hôte`, TTL ~20 min) ; clé de signature jamais dans le payload runner.
- Lab-node : `TrustedUserCAKeys` + `AuthorizedPrincipalsFile` lorsque `ca.pub` est monté ; `AuthorizedKeysFile none`.
- Runner Ansible : `CertificateFile` + agent SSH éphémère (clé privée effacée après `ssh-add`).
- Preuves : `test_f15_ssh_certs.py` (émission, expiration, mauvais périmètre, `lab keys`) ; `test_f15_ssh_lab.py` (SSH réel docker) ; `test_db_roles`. CA d'entreprise : non (D11).

## Prochain jalon

1. Fusionner **F15 (`0022`)** après CI verte.
2. Ensuite selon le backlog (F16–F20, F22–F26, F29–F30, AT02–AT04).
3. F12/F14/F41/F59 à rejouer contre l'IdP d'entreprise au lot 4 (D10, D22).
4. Le lot 2 attend un laboratoire réel autorisé (D2). La double validation de publication attend une décision de Vincent sur l'ADR-0010.
