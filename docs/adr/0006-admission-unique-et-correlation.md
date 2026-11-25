# ADR-0006 : point d'admission unique et identifiant de corrélation

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Entre l'approbation et le démarrage effectif d'un job, beaucoup de choses peuvent changer : droits révoqués, plan expiré, demande modifiée, version de template retirée, ressource résolue dont l'octroi est retiré, stack verrouillée, et plus tard gel d'environnement (F28), fenêtre de changement (F27), disjoncteur de connecteur (F37), droits temporaires (F11), capacité (F05), sauvegarde (F39), réservations (F03), licences (F34), zone de runner (F16), compatibilité des versions (F38), politiques versionnées (F10). Chaque fonction ajoutant son propre contrôle à un endroit différent finirait par en oublier un. De même, la chronologie unifiée (F21) exige que tous les événements d'un changement soient reliés dès le départ.

## Décision

1. **Un seul point de contrôle** : `admission.check(job, now)` appelé par le worker après réclamation et avant tout envoi, pour chaque job sans exception. Aucun autre chemin ne lance un runner.
2. **Registre ordonné de règles** : chaque règle a un identifiant stable, une version, une classe de jobs concernée (`read`, `write` ou toutes) et renvoie `allow` ou `deny` avec un code motif et, si possible, le champ concerné. Les règles du lot 1 et les emplacements réservés sont listés dans `docs/architecture.md` §11. La règle 55 vérifie que la résolution de la révision approuvée reste admissible (version de template non retirée, valeurs résolues encore octroyées) ; elle ne relance jamais le résolveur (ADR-0008).
3. **Décision enregistrée** dans `admission_decisions` (toutes les règles évaluées, version et empreinte du bundle de politiques) et reprise dans l'audit et les événements. Un refus passe le job en `blocked` avec un motif lisible.
4. **Classes de jobs** : chaque type déclare `read` ou `write`. Les règles d'écriture (gel, fenêtre, capacité) ne bloquent jamais inventaire, collecte ou diagnostic.
5. **Politiques versionnées** : au lot 1, un bundle intégré `builtin@1` dont l'empreinte couvre l'identifiant et la version de chaque règle, et des politiques d'environnement versionnées. Ces versions entrent dans la révision et dans le dossier d'exécution ; les changer exige une nouvelle approbation.
6. **Identifiant de corrélation** : `correlation_id = change_id` porté par les événements, jobs, décisions, entrées d'audit, étiquettes des runners et lignes de journal applicatif (variable de contexte structlog). Les opérations hors changement (inventaire) reçoivent leur propre identifiant d'opération. Chaque événement porte aussi `source` (`request`, `approval`, `admission`, `job`, `terraform`, `ansible`, `vcenter`, `verification`, `chat`, `system`) et une `dedupe_key` unique.

## Conséquences

- Ajouter F28 revient à enregistrer une règle `write` et une donnée d'environnement : un job déjà en file sera refusé à son démarrage sans autre changement du worker.
- Ajouter F27 s'appuie sur `jobs.run_after` et sur la revalidation complète déjà faite à l'admission (un plan expiré pendant l'attente est refusé).
- L'admission est sur le chemin critique de chaque job : elle doit rester rapide (lectures indexées, aucune requête réseau externe) ; une règle qui a besoin d'une mesure externe (capacité) lit une observation datée et refuse si elle est trop ancienne.

## Alternatives écartées

- Contrôles à la mise en file seulement : un gel ou une révocation postérieurs ne seraient pas respectés.
- Moteur de politiques externe (OPA) dès le lot 1 : utile pour F10, prématuré avant d'avoir les règles de base ; l'interface du registre le permet plus tard.
