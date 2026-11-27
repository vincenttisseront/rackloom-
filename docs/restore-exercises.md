# Exercices de restauration (F26)

Procédure périodique pour prouver qu'une appliance neuve peut récupérer la base,
les artefacts et les références de secrets, puis vérifier **séparément** les clés
de chiffrement et le state Terraform. Menace A4 du modèle de menaces : une
restauration ne doit pas relancer d'anciennes tâches d'écriture vers le parc.

## Principes

1. **Paquet principal** (ensemble) : base applicative, artefacts chiffrés, références
   de secrets (ciphertext scellé, jamais de clair).
2. **Procédures distinctes** : clés (`ADR-0007`) et state Terraform (`ADR-0002`) —
   inventaire, copie et vérification hors du même geste que la base.
3. **Envois suspendus** : au démarrage d'un exercice (et après une restauration réelle),
   l'interrupteur global `dispatch_paused` est activé. Les jobs d'écriture en file
   restent `queued` avec `wait_reason = dispatch_paused` ; ils ne sont pas réclamés.
   Les jobs de lecture (inventaire, diagnostic) restent possibles.
4. **Reprise humaine** : `rackloom recovery resume` (ou l'API admin) après
   réconciliation ; jamais de bascule automatique.

## Déroulement d'un exercice isolé

Sur une machine de test (répertoire isolé ; lot 3 : VM QEMU neuve) :

```bash
# 1. Produire un paquet depuis l'appliance source
rackloom recovery backup --out /var/tmp/rackloom-backup-$$

# 2. Sur la cible isolée : restauration chronométrée (suspend les envois)
rackloom recovery exercise \
  --backup /var/tmp/rackloom-backup-$$ \
  --isolated /var/tmp/rackloom-restore-$$ \
  --reason "exercice trimestriel de restauration"

# 3. Vérifications séparées (si besoin hors exercice)
rackloom recovery verify-keys --dir /var/tmp/rackloom-backup-$$/keys-separate
rackloom recovery verify-state --dir /var/tmp/rackloom-backup-$$/state-separate/files

# 4. Après réconciliation : reprendre les envois
rackloom recovery resume --reason "exercice terminé, file revue"
```

L'exercice enregistre `duration_ms`, le statut de chaque phase, le nombre de jobs
d'écriture encore en file, et laisse `dispatch_paused` actif jusqu'à reprise explicite.

## Interrupteur seul (maintenance)

```bash
rackloom recovery pause --reason "maintenance réseau" --source maintenance
rackloom recovery status
rackloom recovery resume --reason "fin de maintenance"
```

Équivalents API (rôle `platform_admin`, CSRF) :

- `GET/POST /api/v1/admin/dispatch` · `…/pause` · `…/resume`
- `GET/POST /api/v1/admin/restore-exercises`

## Limites actuelles

| Élément | Niveau |
|---|---|
| Exercice filesystem isolé + chronométrage | testé localement |
| VM QEMU neuve (lot 3) | non vérifié |
| State distant réel | après lot 2 |
| Restauration destructive de l'appliance de prod | hors exercice ; procédure d'exploitation lot 3 |

## Audit

| Action | Entrée |
|---|---|
| Suspension des envois | `dispatch.pause` |
| Reprise | `dispatch.resume` |
| Exercice | `restore.exercise` |
