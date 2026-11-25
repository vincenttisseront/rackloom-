# ADR-0007 : secrets et clés de chiffrement

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Rackloom stocke des identifiants de connecteurs et produit des artefacts sensibles (plans, sorties brutes, rapports). La base doit contenir des références, pas des valeurs en clair. L'API est le composant le plus exposé ; elle doit pouvoir enregistrer un secret sans pouvoir le relire.

## Décision

1. **Références** : les connecteurs stockent `secret_refs` (`local:<uuid>` au lot 1 ; `vault:<chemin>` pour un coffre externe plus tard).
2. **Boîte scellée locale** (PyNaCl `SealedBox`, X25519) : l'API détient la clé publique et chiffre à l'écriture ; seul le worker détient la clé privée et déchiffre au moment du job. Le rôle base `rackloom_api` peut insérer dans `secrets` mais pas lire la colonne chiffrée.
3. **Artefacts** : AES-256-GCM avec clés de fichier. Classe A (plans binaires, sorties brutes filtrées) : clé du worker seulement, jamais téléchargeable. Classe B (rapports et journaux filtrés destinés aux utilisateurs) : clé partagée par le worker et l'API, téléchargement autorisé et audité.
4. **Stockage des clés** : générées par `rackloom keys init` dans un volume `keys` distinct de la base, montées en lecture seule et uniquement dans les services qui en ont besoin ; sur l'appliance, `/etc/rackloom/keys` (racine, 0400). Identifiant de clé enregistré avec chaque chiffré pour permettre la rotation.
5. **Distribution aux runners** : seuls les secrets du job, sur l'entrée standard, pour la durée du job ; lot 2 : identifiants à durée courte lorsque le système cible le permet ; F15 : certificats SSH courts.
6. **Affichage** : un secret n'est jamais renvoyé par l'API ; l'UI montre « défini le … par … » et permet de le remplacer.
7. **Sauvegarde** : les clés ont une procédure de sauvegarde et de restauration distincte de celle de la base (lot 3). Sans la clé, secrets et artefacts sont perdus : c'est voulu.

## Conséquences

- Une compromission de l'API seule ne révèle pas les secrets existants.
- Une compromission du worker révèle les secrets : il fait partie de la base de confiance (voir `docs/threat-model.md`).
- Un coffre externe (HashiCorp Vault, OpenBao, autre) pourra remplacer la boîte locale derrière la même interface `secrets`.

## Alternatives écartées

- Chiffrement symétrique unique partagé par l'API et le worker : l'API pourrait tout relire.
- Secrets en variables d'environnement des conteneurs : visibles par inspection.

## À trancher

- Coffre externe cible, s'il en existe un chez le client.
