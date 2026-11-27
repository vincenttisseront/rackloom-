# SDK d'adaptateurs (F30)

Guide pour ajouter un connecteur (Proxmox, OpenTofu, AWX/AAP, …) sans contourner
les contrôles de périmètre, d'audit et d'approbation.

## Principes

1. **Besoin ≠ technique** : l'adaptateur ne reçoit jamais de champ technique du demandeur.
2. **Même chemin que les connecteurs livrés** : configuration admin, secrets en écriture seule,
   endpoints approuvés, admission, audit, disjoncteur.
3. **Pas d'installation depuis l'UI sans approbation** : un paquet passe
   `submitted` → `approved` → `installed`. Installer un paquet non approuvé renvoie
   `adapter_not_approved`. Le digest doit figurer dans `adapter_trust_digests`.
4. **Approbateur ≠ soumetteur** (`adapter_self_approval`).
5. **Profil production** : aucun mode `simulated`.

## Contrat (`rackloom.connectors.sdk`)

| Élément | Exigence |
|---|---|
| Capacités | Les sept clés `configure`, `test_connection`, `inventory`, `plan`, `execute`, `track`, `cancel` ; mode `real` / `simulated` / `request` ou indisponible |
| Erreurs | Codes stables (`endpoint_not_approved`, `scope_denied`, `tls_untrusted`, …) — jamais un secret |
| Réseau | HTTPS uniquement, vérification du nom d'hôte, **pas** d'URL libre utilisateur |
| Événements | Sources ADR-0006 (`vcenter`, `terraform`, `ansible`, `system`, `job`, `verification`) ; données scrubbées |
| Périmètre | `check_scope` refuse (403) hors `connector_admin` / `project_admin` / `platform_admin` |
| Audit | Chaque mutation (`adapter.*`, `connector.*`) |

L'adaptateur de référence (`rackloom.connectors.reference`) implémente ce contrat sans fournisseur externe.

## Suite de contrat

```bash
cd backend && uv run pytest tests/contract/test_adapter_contract.py
# ou
GET /api/v1/admin/adapters/contract
```

Un descripteur avec `enforces_scope=False`, une URL libre, ou un `check_scope` no-op **échoue** la suite.

## Ajouter Proxmox / OpenTofu / AWX

1. **Implémenter** un module sous `connectors/` (ou paquet versionné) exposant le protocole `Adapter`.
2. **Déclarer** le manifeste `rackloom.adapter_package.v1` (slug, version, kind `proxmox`|`opentofu`|`awx`, capacités, réseau).
3. **Enregistrer** le digest SHA-256 canonique dans `adapter_trust_digests` (hors bande ou procédure admin).
4. **Soumettre** via `POST /api/v1/admin/adapters/submit`.
5. **Faire approuver** par un autre administrateur de plateforme.
6. **Installer** via `POST /api/v1/admin/adapters/{id}/install` — crée le connecteur.
7. **Passer** la suite de contrat (descripteur + instance).

### Proxmox

- Kind `proxmox` ; inventaire / exécution via API HTTPS vers des endpoints approuvés.
- Pas de second chemin d'écriture qui contourne Terraform pour les ressources qu'il possède.
- Capacités absentes affichées « non disponible » jusqu'à implémentation réelle.

### OpenTofu

- Kind `opentofu` ; même modèle que le connecteur Terraform (`plan` / `execute` sur plan SHA-256 approuvé).
- Lockfile et miroir de providers ; jamais de HCL fourni par l'utilisateur.

### AWX / AAP

- Kind `awx` ; lance des jobs sur un playbook / inventaire **déjà** dans le catalogue Rackloom.
- Aucune commande libre ; variables issues de la configuration déclarée des composants.

## API

| Méthode | Route | Effet |
|---|---|---|
| GET | `/admin/adapters` | Liste paquets + digests de confiance |
| GET | `/admin/adapters/contract` | Rapport de la suite |
| POST | `/admin/adapters/submit` | Soumet (ou `demo_reference: true`) |
| POST | `/admin/adapters/{id}/approve` | Approuve (≠ soumetteur) |
| POST | `/admin/adapters/{id}/reject` | Rejette avec motif |
| POST | `/admin/adapters/{id}/install` | Installe si `approved` |

## Hors périmètre v1

- Place de marché d'extensions, téléchargement automatique depuis Internet.
- Exécution de code d'adaptateur non revu dans le worker (l'entrée reste du code livré avec l'appliance).
