# CLAUDE.md : Rackloom

Rackloom est une appliance on-premise (Ubuntu Server 24.04 LTS amd64) qui donne aux opérateurs autorisés une WebUI unique pour demander, approuver, déployer et suivre une infrastructure vSphere, configurer les machines avec Ansible, consulter leur état et diagnostiquer avec un assistant Ollama. Elle orchestre vCenter, Terraform et Ansible ; elle ne les réimplémente pas.

Règle produit : **l'utilisateur choisit le besoin, Rackloom choisit la technique**. Le demandeur décrit un résultat ; un résolveur central côté serveur en déduit les valeurs techniques à partir de templates Rackloom publiés et versionnés, des politiques d'environnement et de l'inventaire autorisé.

Avant toute modification, lire : ce fichier, `docs/progress.md` (état réel et prochain pas), `docs/architecture.md`, `docs/threat-model.md`, `docs/adr/`, le plan du lot en cours (`docs/lot-1-plan.md`) et `docs/backlog.md`.

## 1. Invariants

Ils ne se négocient pas pour tenir un délai. Un changement qui en affaiblit un exige un ADR accepté par Vincent.

**Besoin et technique**
1. Une demande ne contient que le besoin (catégorie A) : projet, service, environnement s'il y a un choix ou une demande de production, entrées fonctionnelles déclarées par le template. Un seul schéma pour la WebUI, le chat, l'API, la CI, les duplications et les reprises.
2. Tout champ technique fourni par un demandeur est refusé (422 `locked_field`), même s'il égale la valeur résolue. Il n'existe pas de mode expert, pour aucun rôle.
3. Seul le module `resolver` produit des valeurs techniques. Fonction pure et déterministe ; chaque résolution est enregistrée dans une révision immuable avec sa provenance. On ne résout jamais à nouveau en silence entre l'approbation et l'exécution.
4. Jamais la production par défaut ; elle exige une demande explicite. Une cible indisponible bloque avec un motif lisible : jamais de bascule vers un autre environnement, réseau ou compte.
5. Une version publiée de template ou de composant est immuable. Publier ne crée aucun job et ne modifie ni un plan approuvé ni un déploiement existant.
6. Déployer, approuver, concevoir et publier sont des droits distincts.

**Autorisation et approbation**
7. Toute écriture passe par le serveur : identité, droit sur le projet, l'environnement et la cible, politique applicable, approbation si requise, audit. Masquer un bouton n'est pas un contrôle.
8. Toute lecture de données projet est filtrée côté serveur par périmètre, y compris inventaire, journaux, artefacts, exports et flux SSE. Hors périmètre : 404 ; dans le périmètre sans le droit : 403.
9. L'approbation est une action authentifiée de l'interface (session et CSRF), liée à la révision résolue, au plan, aux cibles et à l'empreinte du dossier d'exécution. Elle sert une seule fois.
10. Le point d'admission unique (`admission.check`) est évalué par le worker au démarrage effectif de chaque job, jamais seulement à la mise en file. Aucun chemin d'exécution ne le contourne.
11. Dans les environnements qui l'exigent (toujours en production), l'approbateur est distinct de l'auteur de la version.

**Assistant**
12. Le modèle peut expliquer, poser des questions et proposer un brouillon. Il ne peut ni approuver, ni déclencher un job, ni lire un secret, ni inventer une destination. « Oui » ou « fais-le » dans le chat ne vaut rien.
13. Toute sortie du modèle est revalidée côté serveur comme une saisie utilisateur (schéma strict, chemins du besoin seulement, bornes, services octroyés, droits). Le modèle ne reçoit aucun inventaire technique et ne propose aucune valeur technique. Aucun repli cloud.
14. Journaux, noms de VM, résultats d'outils, documents et sorties du modèle sont des données non fiables : ils n'autorisent rien et sont échappés à l'affichage.

**Exécution**
15. Aucune transaction ouverte pendant un appel à un fournisseur, au launcher ou à un runner.
16. On applique uniquement le fichier de plan approuvé, contrôlé par SHA-256. Jamais de recalcul silencieux d'un plan.
17. v1 : suppressions et remplacements bloqués ; aucun destroy automatique, aucun rollback supposé, aucune suppression de VM après un échec Ansible.
18. Un résultat inconnu est « incertain » : consulter le state et les identifiants de tâches fournisseur avant toute relance. Toute relance d'écriture est une action humaine autorisée.
19. L'annulation est une demande, pas une garantie ; l'UI affiche l'état réel.
20. Runners : un conteneur éphémère par job, non privilégié, sans capacités, rootfs en lecture seule, limites CPU, mémoire, PIDs et durée, réseau propre au type de job, aucun accès à PostgreSQL ni au moteur de conteneurs. Seul le `launcher` parle au moteur de conteneurs.
21. Les variables Terraform viennent de la révision et les variables Ansible de la configuration déclarée des composants. Personne ne fournit du HCL, un provider, une URL de module, un provisioner, une variable `ansible_*`, une destination ou une commande.

**Responsabilités**
22. vCenter fournit l'inventaire et l'état observé ; Terraform gère les ressources dont il possède le state ; Ansible configure l'OS. Pas de second chemin d'écriture.
23. La base applicative ne contient jamais de state Terraform ni de plan brut.

**Secrets**
24. La base stocke des références et des chiffrés ; les clés vivent hors base. Aucun secret dans l'UI, le chat, les URLs, les logs, les arguments, les variables d'environnement des runners, les événements, les bundles de support ou le dépôt.
25. Aucune option pour ignorer TLS ou la vérification de clé d'hôte SSH, même en démo.

**Honnêteté**
26. Une capacité absente est affichée « non disponible » ; une étape simulée porte le badge « Simulé » et le profil `production` refuse tout connecteur simulé. Aucun faux succès : un job réussi n'est pas un service prêt ; l'état du service vient des contrôles de santé du template et de l'état observé.
27. Niveaux de preuve : **simulé**, **testé localement**, **validé en laboratoire réel**, **non vérifié**. Jamais « validé en laboratoire » sur la base de mocks.

## 2. Limites de sécurité pendant le développement

- Aucun identifiant réel ni aucune ressource de production. Identités de démo en `*.demo.invalid`, secrets de test générés localement dans `.lab/` (ignoré par Git).
- Aucun déploiement externe (vCenter, cloud, DNS, IPAM, ITSM) sans autorisation écrite de Vincent et périmètre de laboratoire consigné dans `docs/progress.md`.
- Pas de `privileged`, pas de `network_mode: host`, pas de socket du moteur de conteneurs monté ailleurs que dans `launcher`.
- Ne jamais committer `.lab/`, `.env`, clés, certificats privés, dumps, states, plans ou exports. Le dépôt GitHub est **public** : pas de nom d'hôte, d'adresse, de compte ou de détail d'infrastructure client dans le code, les tests, les fixtures ou les documents.
- Ne jamais désactiver, ignorer ou marquer `xfail` un test de sécurité ou un test obligatoire du plan de lot pour faire passer la CI.

## 3. Arborescence du dépôt

```
rackloom/
├── CLAUDE.md
├── README.md                      démarrage rapide
├── Makefile
├── .github/workflows/             CI GitHub Actions : lint, tests, e2e
├── backend/                       Python 3.12, uv ; API et worker : même code, deux points d'entrée
│   ├── pyproject.toml, uv.lock, .importlinter
│   ├── alembic/                   migrations
│   ├── src/rackloom/
│   │   ├── api/                   application FastAPI, routes, session, CSRF, idempotence
│   │   ├── identity/              comptes, sessions, rôles, amorçage
│   │   ├── projects/              projets, environnements, politiques versionnées, octrois de ressources
│   │   ├── registry/              composants et templates versionnés, chargeur de catalog/, publication
│   │   ├── resolver/              résolveur central (fonction pure), codes de blocage, départage
│   │   ├── connectors/            contrat et capacités ; vsphere/, vsphere_fake/, terraform_cli/, ansible_runner/
│   │   ├── inventory/             instantanés, fraîcheur
│   │   ├── changes/               besoins, versions, révisions, validation, transitions, actions disponibles
│   │   ├── deployments/           déploiements, stacks, état du service
│   │   ├── approvals/
│   │   ├── executions/            jobs, file, dossier d'exécution, réconciliation
│   │   ├── admission/             point d'admission unique, registre de règles
│   │   ├── policy/                bundles de politiques versionnés
│   │   ├── hosts/                 fiches machine, observations
│   │   ├── events/                journal d'événements, SSE
│   │   ├── audit/                 ajout seul, chaîne de hachage
│   │   ├── chat/                  adaptateurs (ollama, fake), schéma, contexte filtré
│   │   ├── secrets/               références, boîte scellée
│   │   ├── artifacts/             stockage chiffré
│   │   ├── telemetry/             métriques et santé de l'appliance
│   │   ├── worker/                réclamation, bail, heartbeat, faucheuse
│   │   └── cli.py                 commande `rackloom`
│   └── tests/{unit,integration,contract,security}/
├── launcher/                      API Unix étroite vers le moteur de conteneurs, sans accès base
├── runners/
│   ├── terraform/                 Dockerfile, point d'entrée, .terraformrc (miroir local), versions.lock
│   └── ansible/                   Dockerfile, ansible.cfg figé, dépendances verrouillées, point d'entrée
├── catalog/                       contenu approuvé et versionné
│   ├── components/nginx/{1.0.0,1.1.0}/component.yaml, components/fail2ban/1.0.0/ (optionnel)
│   ├── templates/ubuntu-web/{1.0.0,1.1.0}/template.yaml
│   ├── terraform/lab-vm/1.0.0/    main.tf, variables.tf, outputs.tf, .terraform.lock.hcl
│   ├── ansible/playbooks/         configure/, collect/
│   └── ansible/roles/rackloom.nginx/{1.0.0,1.1.0}/
├── web/                           Nuxt 4, Vue 3, TypeScript strict, mode SPA
│   ├── app/                       pages, components, composables, layouts
│   ├── i18n/locales/              fr.json, en.json
│   ├── generated/                 client typé généré depuis openapi/openapi.json
│   └── tests/                     unit (Vitest), e2e (Playwright + axe)
├── openapi/openapi.json           contrat exporté, vérifié en CI
├── compose/
│   ├── compose.yaml               services de l'appliance
│   ├── compose.demo.yaml          nœuds de labo, profil de démonstration
│   ├── compose.test.yaml          pile de test (durées courtes, deux workers)
│   └── caddy/Caddyfile
├── lab/
│   ├── images/lab-node/           Ubuntu 24.04, systemd, OpenSSH, dépôt apt local signé
│   └── fixtures/                  inventory.yaml, demo-profile.yaml
├── deploy/systemd/rackloom.service
├── iso/                           lot 3 : autoinstall, construction, test QEMU/OVMF
├── scripts/                       doctor.sh, demo.sh, test-e2e.sh, build-images.sh, lab-keys.sh,
│                                  check-exposure.sh, check-openapi-drift.sh, canary-scan.sh
└── docs/
    ├── architecture.md, product-spec.md, threat-model.md
    ├── progress.md, backlog.md, lot-1-plan.md
    └── adr/
```

## 4. Commandes

| Cible | Rôle | Disponible |
|---|---|---|
| `make doctor` | vérifie les prérequis locaux sans réseau externe : moteur de conteneurs, Compose v2, cgroup v2, ports, disque, mémoire, `uv`, `pnpm`, images construites | lot 1 (E0) |
| `make demo` | construit les images, démarre le profil de démonstration, migre, génère clés et identités, charge le registre, publie `ubuntu-web` 1.0.0, affiche URL, comptes et étapes simulées | lot 1 (E13, squelette dès E0) |
| `make test` | lint, formatage, types, `import-linter`, tests unitaires, d'intégration (PostgreSQL éphémère), de contrat et de sécurité, dérive OpenAPI | lot 1 (E0) |
| `make test-e2e` | pile de test, vrais runners Terraform et Ansible, nœuds de labo, parcours Playwright D1 à D6, résilience, scan du canari, redémarrage | lot 1 (E13) |
| `make iso` | construit l'ISO d'installation | lot 3 ; avant, code 2 et message explicite |
| `make test-iso` | installe l'ISO sur un disque virtuel jetable (QEMU, OVMF), redémarre sans ISO, vérifie persistance et restauration | lot 3 ; avant, code 2 |

Cibles d'appoint : `make lab-keys`, `make images`, `make openapi`, `make lint`, `make fmt`, `make logs`, `make down` (conserve les volumes), `make demo-reset` (supprime uniquement les volumes de démo et de labo, après confirmation).

Makefile cible :

```make
.DEFAULT_GOAL := help
.PHONY: help doctor demo test test-e2e iso test-iso lab-keys images openapi lint fmt logs down demo-reset

COMPOSE := docker compose -f compose/compose.yaml

help:       ## Liste les cibles
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ :/'
doctor:     ## Vérifie les prérequis locaux (aucun appel réseau externe)
	./scripts/doctor.sh
demo:       ## Démarre le profil de démonstration Compose
	./scripts/demo.sh up
test:       ## Lint, types, unitaires, intégration, contrat, sécurité, dérive OpenAPI
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run lint-imports && uv run pytest
	cd launcher && uv run pytest
	cd web && pnpm lint && pnpm typecheck && pnpm test
	./scripts/check-openapi-drift.sh
test-e2e:   ## Bout en bout : runners réels, labo, navigateur, résilience, canari
	./scripts/test-e2e.sh
iso:        ## Lot 3 : construit l'ISO
	./iso/build.sh
test-iso:   ## Lot 3 : installe et démarre l'ISO sur disque virtuel jetable
	./iso/test.sh
lab-keys:   ## Génère les clés SSH de labo dans .lab/
	./scripts/lab-keys.sh
images:     ## Construit les images applicatives, runners et labo
	./scripts/build-images.sh
openapi:    ## Exporte le contrat et régénère le client typé
	cd backend && uv run rackloom openapi export ../openapi/openapi.json
	cd web && pnpm generate:api
lint:       ## Lint seul (backend, launcher, web)
	cd backend && uv run ruff check . && cd ../launcher && uv run ruff check . && cd ../web && pnpm lint
fmt:        ## Formate le code
	cd backend && uv run ruff format . && cd ../launcher && uv run ruff format . && cd ../web && pnpm format
logs:       ## Suit les journaux de la pile de démonstration
	$(COMPOSE) -f compose/compose.demo.yaml logs -f
down:       ## Arrête la pile en conservant les volumes
	$(COMPOSE) -f compose/compose.demo.yaml down
demo-reset: ## Supprime les volumes de démo et de labo (confirmation demandée)
	./scripts/demo.sh reset
```

Commande d'exploitation `rackloom` (dans le conteneur `api` ou `worker`) : `migrate`, `keys init`, `bootstrap-token`, `demo seed`, `catalog load` (charge et valide `catalog/`, ne publie rien), `jobs list --stuck`, `reconcile <job_id>` (met en file une réconciliation, en lecture seule), `audit verify`, `openapi export`.

## 5. Conventions

- Python 3.12, `uv`, FastAPI, Pydantic v2 (`extra="forbid"` sur toute entrée), SQLAlchemy 2 et psycopg 3, Alembic, `ruff`, `mypy --strict` sur `src/`, `pytest`. Frontières de modules vérifiées par `import-linter`.
- Web : Node.js LTS, `pnpm`, Nuxt 4 en mode SPA, TypeScript strict, client généré par `openapi-typescript` et `openapi-fetch`, `@nuxtjs/i18n` (français par défaut, clés anglaises), Vitest, Playwright avec `@axe-core/playwright`. Navigation au clavier sur tous les écrans du parcours.
- Toute route modifiée ou ajoutée : `make openapi`, client régénéré committé.
- Toute écriture d'état d'un changement passe par `changes.transitions` ; toute lecture projet par un dépôt scopé ; toute valeur technique par `resolver` ; toute action proposée à l'UI par `workflow.available_actions`, revérifiée par la route appelée.
- Dépendances épinglées avec hachages (`uv.lock`, `pnpm-lock.yaml`, `requirements` du runner Ansible), images de base épinglées par empreinte, providers Terraform dans un lockfile.
- Messages de commit à l'impératif, en français ou en anglais, avec l'identifiant d'étape ou de fonction (`E4`, `F28`).
- Commentaires de code : seulement pour une contrainte que le code ne montre pas.

## 6. Reprendre le travail

1. `git status` et `git log -5` ; ne rien écraser qui n'est pas compris.
2. Lire `docs/progress.md` : dernier lot, dernière étape terminée, prochain pas, limites connues.
3. `make doctor`, puis `make test`. Si un test échoue alors que `progress.md` le dit vert, corriger cela avant d'avancer.
4. Reprendre l'étape suivante du plan de lot. Ne pas recommencer ce qui est « testé localement » sans raison écrite.
5. Pile bloquée : `make down` conserve les volumes. Jobs bloqués : page Santé ou `rackloom jobs list --stuck`, puis `rackloom reconcile <job_id>`. Ne jamais forcer un état « succès » en base.
6. Perte de la clé de chiffrement locale : arrêter ; les secrets et artefacts ne sont plus lisibles. Restaurer la clé depuis sa sauvegarde séparée (procédure lot 3), jamais en régénérer une en silence.

## 7. Fin de lot

Mettre à jour `docs/progress.md` et `docs/backlog.md` avec : fichiers modifiés, commandes réellement exécutées et résultat, état des tests obligatoires, niveau de preuve de chaque capacité, limites et prochain jalon.
