# Modèle de sécurité — packaging ISO (Lot 3)

Complète `docs/threat-model.md` et ADR-0004 / ADR-0007 pour le média d’installation.

## Installateur

- Seul Subiquity / Curtin Ubuntu 24.04 partitionne et installe le système.
- Confirmation disque = interaction Subiquity (`interactive-sections: storage`).
- Pas d’UI maison pouvant contourner les garde-fous Curtin.

## Secrets

- ISO publiée : **aucun** mot de passe, jeton, clé privée, certificat privé.
- Mot de passe console : saisi dans Subiquity identity (jamais dans `user-data` publié).
- Graine `user-data.test` : hash lab local uniquement, jamais publiée.
- Firstboot génère secrets DB et TLS localement (0400 / 0640).

## Surface après install

- Exposition : HTTPS via Caddy uniquement (voir ADR-0004).
- Runners isolés (ADR-0003) ; aucun socket moteur hors `launcher`.
- Audit chaîné ; lectures projet scopées (invariants CLAUDE.md).

## Médias et intégrité

- ISO amont : SHA-256 + GPG Ubuntu CD Image Signing Key.
- Artefact Rackloom : `.sha256` + manifeste des images embarquées.
