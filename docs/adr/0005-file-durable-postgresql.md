# ADR-0005 : file durable dans PostgreSQL

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Les jobs doivent survivre aux redémarrages, ne pas être exécutés deux fois par deux workers, et ne jamais être relancés à l'aveugle après un envoi au résultat inconnu. Aucune transaction ne doit rester ouverte pendant un appel au fournisseur. On veut éviter un courtier de messages en v1.

## Décision

1. **Table `jobs`** dans la base applicative ; création dans la même transaction que la transition d'état, l'événement et l'audit qui la motivent.
2. **Réclamation** en transaction courte : `SELECT … WHERE state = 'queued' AND run_after <= now() ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1`, puis `state = 'leased'`, `lease_owner`, `lease_expires_at = now() + 60 s`, `lease_epoch = lease_epoch + 1`.
3. **Heartbeat** toutes les 15 s : `UPDATE … SET lease_expires_at = … WHERE id = $1 AND lease_epoch = $2`. Zéro ligne mise à jour signifie bail perdu : le worker arrête d'écrire pour ce job (jeton de clôture). Toutes les écritures du worker sur le job portent la condition d'époque.
4. **Admission** après réclamation (ADR-0006), puis `dispatched_at = now()` **avant** l'appel au launcher pour les jobs d'écriture.
5. **Faucheuse** toutes les 10 s sur les baux expirés : job `read` ou job `write` sans `dispatched_at` → remis en file (`attempts + 1`, `run_after` avec recul exponentiel, `max_attempts`) ; job `write` avec `dispatched_at` → `uncertain` et job `reconcile` créé. Jamais de relance automatique d'une écriture envoyée.
6. **Notifications** : `pg_notify` après validation de la transaction (identifiant seulement) pour réveiller les workers et les flux SSE ; l'interrogation périodique reste le filet de sécurité.
7. **Événements** : table `events` en ajout seul, `dedupe_key` unique pour rendre idempotente toute réémission.
8. **Workers** : table `workers` (heartbeat, emplacements, occupation) pour la page Santé et les métriques.

## Conséquences

- Pas de garantie « exactement une fois » de bout en bout : au plus un envoi par décision d'admission, réconciliation avant toute relance.
- Débit suffisant pour une appliance (dizaines de jobs simultanés) ; à revoir si l'usage dépasse cet ordre de grandeur.
- Durées de bail et de heartbeat injectables pour des tests de résilience rapides.

## Alternatives écartées

- RabbitMQ, Redis, Kafka : un composant de plus à exploiter, sauvegarder et sécuriser, sans gain pour le volume visé.
- Bibliothèques de tâches (Celery) : sémantique d'accusé et de relance mal adaptée aux écritures au résultat incertain.
