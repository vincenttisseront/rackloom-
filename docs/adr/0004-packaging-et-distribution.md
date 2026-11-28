# ADR-0004 : packaging et distribution

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Le produit est livré sous forme d'ISO Ubuntu Server 24.04 LTS amd64. Il doit être simple à exploiter, fonctionner en mode déconnecté, n'exposer que HTTPS et ne jamais effacer un disque sans confirmation explicite.

## Décision

1. **Exécution** : Docker Compose v2 sur Ubuntu 24.04, lancé par l'unité systemd `rackloom.service` (`Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=docker compose up -d --wait`, `ExecStop=docker compose down`, sans `-v`). Politique `restart: unless-stopped` sur les services.
2. **Exposition** : seul le proxy publie un port (443 sur l'interface d'administration ; `127.0.0.1:8443` en développement). PostgreSQL, Ollama, launcher et runners restent sur des réseaux internes.
3. **Proxy** : Caddy 2, TLS avec certificat fourni par l'admin ou CA interne générée au premier boot, ACME désactivé, HSTS, CSP stricte, `flush_interval -1` pour SSE. La WebUI est générée en mode SPA et servie en fichiers statiques.
4. **Images** : construites par le pipeline, images de base épinglées par empreinte, référencées par empreinte dans `compose.yaml` ; aucun `latest`.
5. **ISO (lot 3)** : ISO amont vérifiée (`SHA256SUMS` et sa signature), reconditionnée avec une configuration autoinstall. L'opérateur ne voit **aucune** UI Subiquity stock : un installateur console Rackloom (`early-commands`, `iso/installer/`) demande uniquement réseau, hostname/nom, mot de passe du compte initial, et une **confirmation explicite du disque** (retaper le chemin) ; il réécrit ensuite `/autoinstall.yaml` en mode 100 % non interactif. Sous le capot : live-server Ubuntu 24.04 + Subiquity/curtin. L'ISO contient les images de l'application et des runners, le miroir de providers, les collections Ansible et le dépôt de paquets nécessaires ; tout le reste (modèles Ollama, templates, paquets supplémentaires) s'importe depuis un bundle signé ou un miroir approuvé (F17). Aucun téléchargement imprévu au premier boot.
6. **Premier boot** : reprend le hostname choisi à l'install (sauf `RACKLOOM_FQDN`), valide réseau/DNS/NTP, génère TLS, clés et jeton d'amorçage, puis affiche l'URL. Aucun secret, mot de passe ou certificat privé embarqué.
7. **Publication** : ISO, SHA-256, manifeste des composants et versions, SBOM (Syft, format SPDX ou CycloneDX), procédure d'installation. Démarrage prouvé en UEFI (QEMU + OVMF) sur disque virtuel jetable, puis premier boot après retrait de l'ISO. BIOS et Secure Boot testés séparément seulement s'ils sont annoncés.
8. **Mises à jour** : remplacement des images et migrations de base ; données, clés et states préservés ; aucune mise à jour ne crée de job ni ne modifie le parc.
9. **Sauvegarde** : base, artefacts et références de secrets ensemble ; clé de chiffrement et state Terraform par des procédures distinctes. Récupération documentée depuis une appliance neuve, y compris quand l'ancienne tournait sur le vCenter qu'elle administre.

## Conséquences

- `make iso` et `make test-iso` n'existent réellement qu'au lot 3 ; avant, ils échouent explicitement.
- Les licences de chaque composant de l'ISO (Terraform, providers, modèles, images) sont vérifiées avant redistribution (voir ADR-0002).

## Alternatives écartées

- Paquets `.deb` natifs : plus de travail de packaging pour chaque service, isolation moindre.
- Kubernetes (k3s) : complexité d'exploitation non justifiée en v1.
- Image disque préinstallée (OVA) seule : utile plus tard, mais l'ISO est exigée.

## À trancher

- Registre d'images (GHCR proposé, cohérent avec le dépôt GitHub) et exécuteurs CI capables de lancer la pile Compose et les tests de boot QEMU.
- Annonce ou non du support BIOS et Secure Boot.
