# Avancement de Rackloom

## État au 2026-11-25

- **Lot 0 (cadrage)** : rédigé. Documents seulement.
- **Lot 1 (parcours Compose avec résolveur central et modèle template/composant)** : non commencé. Prochaine étape : E0 de `docs/lot-1-plan.md`.
- **Rien n'est implémenté.** Aucun code, aucune image, aucun test. Aucune commande de construction ou de test n'a été exécutée : le dépôt est vide et `make` n'existe pas encore.
- Pas de laboratoire réel pour l'instant (D2 et D21, décidé) : VM locales jetables et observations fictives explicites ; aucun déploiement externe n'est autorisé.

## Capacités

| Capacité | Statut | Niveau de preuve |
|---|---|---|
| Authentification locale, sessions, CSRF, rôles par projet (dont publicateur de templates) | non implémenté | non vérifié |
| Besoin versionné, rejet des champs techniques, résolveur central, révisions avec provenance | non implémenté | non vérifié |
| Registre de composants et templates versionnés, publication, dépréciation, retrait | non implémenté | non vérifié |
| Parcours en trois étapes, formulaire généré, « Détails du plan » en lecture seule | non implémenté | non vérifié |
| Machine à états, événements, audit chaîné | non implémenté | non vérifié |
| File PostgreSQL, bail, heartbeat, réconciliation | non implémenté | non vérifié |
| Point d'admission unique (dont règle 55) | non implémenté | non vérifié |
| Launcher et runners isolés | non implémenté | non vérifié |
| Plan et application Terraform (ressource locale jetable, une stack par déploiement) | non implémenté | non vérifié |
| Approbation liée à la révision et à l'empreinte | non implémenté | non vérifié |
| Ansible sur nœuds de labo, vérification, état du service distinct du job, fiches machine | non implémenté | non vérifié |
| SSE et chronologie | non implémenté | non vérifié |
| Assistant (adaptateur fictif, adaptateur Ollama) | non implémenté | non vérifié |
| Connecteur vCenter (inventaire réel) | lot 2 | non vérifié |
| ISO, premier boot, sauvegarde, restauration | lot 3 | non vérifié |
| OIDC | lot 4 | non vérifié |
| Fonctions F01 à F60 et atelier AT01 à AT04 | voir `docs/backlog.md` (toutes « à faire ») | non vérifié |

Démonstrations D1 à D6 et tests obligatoires T01 à T41 (`docs/lot-1-plan.md` §1.2 et §9) : non écrits.

## Limites connues

- Les choix techniques suivent les briefs faute de dépôt existant ; les ADR sont au statut « proposé » tant que Vincent ne les a pas acceptés.
- Les versions exactes (Terraform, providers, ansible-core, Nuxt, images de base) seront fixées et épinglées à l'étape qui les introduit.
- Hypothèses du lot 1 : `docs/lot-1-plan.md` §3.
- Le lot 1 ne livre ni l'atelier de templates, ni F51 à F60 ; il en pose seulement les bases (`docs/architecture.md` §12 et §12.1).

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
| D1 | CI et registre d'images pour le dépôt GitHub `vincenttisseront/rackloom-` (public ; tiret final volontaire ?) | dépôt public conservé, nom `rackloom-` conservé, GitHub Actions et GHCR | E0 (pipeline) | décidé (Vincent, 2026-11-25) |
| D2 | Périmètre et date d'un laboratoire réel autorisé (vCenter, version, comptes, réseau, image Ubuntu) | pas de laboratoire réel pour l'instant ; VM locales jetables et observations fictives explicites | lot 2, AT01 sur VM jetables, F60, toute mention « validé en laboratoire » | décidé (Vincent, 2026-11-25) |
| D3 | Redistribution de Terraform (BSL 1.1) et des providers dans l'ISO ; qualification éventuelle d'OpenTofu | avis juridique avant le lot 3 | lot 3 | ouverte |
| D4 | Backend de state distant réel | stockage objet compatible S3 interne avec verrouillage natif | lot 2 | ouverte |
| D5 | Moteur des runners sur l'appliance | Podman sans root (ADR-0003) | lot 3 | ouverte |
| D6 | Une reprise ciblée exige-t-elle une nouvelle approbation ? | oui (approbation de type `retry`) | E9 | ouverte |
| D7 | L'administrateur de plateforme lit-il les données des projets sans rôle projet ? | l'administrateur de plateforme n'a pas accès aux données d'un projet sans rôle attribué et audité | E1 | décidé (Vincent, 2026-11-25) |
| D8 | Qui peut geler un environnement (F28) | administrateurs de plateforme et approbateurs de l'environnement | F28 | ouverte |
| D9 | Calage du lot 2 (laboratoire réel) et du lot 3 (ISO) dans l'ordre décidé | lot 2 dès qu'un laboratoire existe, pour valider F21, F05, F03, F09 et AT01 ; lot 3 avant F17 et F26 ; OIDC avant F12, F14, F41 et F59 | planification | ouverte |
| D10 | IdP cible et second facteur local avant le lot 4 | à préciser | F12, F14, F41, F59, usage hors labo | ouverte |
| D11 | Outils tiers du backlog : IPAM (F03), ITSM (F25), CA SSH (F15), fabrication d'images (F07), sauvegarde (F39), licences (F34) | à préciser selon l'existant | ces fonctions | ouverte |
| D12 | Départage du résolveur entre candidats équivalents (H13) | rang dans la liste du template, puis identifiant stable ; aucune question tant qu'il n'y a qu'un site | E4, F32 | ouverte |
| D13 | Environnement par défaut quand plusieurs environnements hors production sont admissibles (H14) | l'utilisateur choisit parmi eux ; jamais la production | E4 | ouverte |
| D14 | Date de fin exigée en `dev` (H16) | date de fin obligatoire en `dev`, 30 jours au plus ; seule question du scénario D1 | E2, E4 | décidé (Vincent, 2026-11-25) |
| D15 | Composant Fail2ban | optionnel en E10 si le reste est vert, sinon livré avec AT01 | E10 | ouverte |
| D16 | Qui tient le rôle `template_publisher` ; double validation pour publier | une seule personne publie au lot 1 ; la double validation est étudiée avec AT01 | E3, AT01 | décidé (Vincent, 2026-11-25) |
| D17 | Référent affiché dans les messages de blocage | référent désigné par projet (`projects.referent_user_id`) | E2, E4 | ouverte |
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
- Prochain jalon : lot 1, étape E0 (socle du dépôt, `make doctor`, `make test`, pile Compose minimale, contrôle d'exposition).
