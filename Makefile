.DEFAULT_GOAL := help
.PHONY: help doctor venv images demo down demo-reset logs test typecheck test-e2e smoke check-exposure \
        lab-keys openapi lint fmt iso test-iso

SHELL := /bin/bash
COMPOSE := docker compose -f compose/compose.yaml -f compose/compose.demo.yaml

help:           ## Liste les cibles
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ :/'
doctor:         ## Vérifie les prérequis locaux (aucun appel réseau externe)
	./scripts/doctor.sh
venv:           ## Installe les dépendances de développement (backend/.venv, web/node_modules)
	cd backend && uv sync --frozen
	cd web && npm ci
images:         ## Construit les images applicatives, runners et labo (étiquette :dev, rien n'est poussé)
	./scripts/build-images.sh
demo:           ## Démarre la démonstration locale sur https://localhost:8443
	./scripts/demo.sh
down:           ## Arrête la pile en conservant les volumes
	$(COMPOSE) --profile lab down
demo-reset:     ## Supprime les volumes rackloom_* (base, clés, labo), après confirmation
	@read -r -p "Supprimer tous les volumes rackloom_* (base, clés, labo) ? Taper « oui » : " answer; \
	  [ "$$answer" = oui ] || { echo "abandon"; exit 1; }
	$(COMPOSE) --profile lab --profile tools down -v --remove-orphans
logs:           ## Suit les journaux de la pile
	$(COMPOSE) --profile lab logs -f
test:           ## Lint, contrats d'imports, tests backend (unitaires, intégration, sécurité), typecheck web, dérive OpenAPI
	cd backend && uv run ruff check . ../launcher/src ../scripts && uv run lint-imports && uv run pytest
	cd web && npx nuxi typecheck
	./scripts/check-openapi-drift.sh
	./iso/branding/test-apply-branding.sh
	./iso/installer/test-installer.sh
typecheck:      ## mypy sur backend/src (non bloquant au lot 1 : dette chiffrée dans docs/progress.md)
	cd backend && uv run mypy
test-e2e:       ## Bout en bout sur pile jetable : runners réels, labo, Playwright D1 à D6, T25, canari T24, T28
	./scripts/test-e2e.sh
smoke:          ## Parcours D1 par l'API contre la pile de démonstration en cours
	uv run --project backend python scripts/smoke-d1.py
check-exposure: ## T28 : seul 127.0.0.1:8443 est publié
	./scripts/check-exposure.sh
lab-keys:       ## Génère les identités SSH de labo si absentes (privées scellées en base, publiques dans lab-keys)
	$(COMPOSE) run --rm cli lab keys --pub-dir /lab-keys --if-missing
openapi:        ## Exporte le contrat et régénère le client typé
	cd backend && uv run rackloom openapi export --output ../openapi/openapi.json
	cd web && npm run gen:api
lint:           ## Lint seul (backend, launcher, scripts)
	cd backend && uv run ruff check . ../launcher/src ../scripts && uv run lint-imports
fmt:            ## Trie les imports et applique les corrections sûres de ruff
	cd backend && uv run ruff check --fix . ../launcher/src ../scripts
iso:            ## Lot 3 : construit l'ISO appliance (Ubuntu 24.04 autoinstall ; Windows : iso/build.ps1)
	./iso/build.sh
test-iso:       ## Lot 3 : installe l'ISO sur disque virtuel jetable (QEMU + OVMF)
	./iso/test.sh
