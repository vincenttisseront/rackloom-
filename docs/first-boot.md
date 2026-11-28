# Premier boot de l'appliance

Unité : `rackloom-firstboot.service` → `/usr/local/sbin/rackloom-firstboot`
→ `/usr/local/lib/rackloom/firstboot.sh`.

## Comportement

1. **Idempotent** : si `/var/lib/rackloom/firstboot.done` existe, sortie 0.
2. Vérifie une route IPv4 par défaut (sinon échec lisible ; relancer après config réseau).
3. DNS / NTP : best-effort (mode déconnecté toléré ensuite).
4. Hostname : `RACKLOOM_FQDN` ou hostname déjà posé par **Subiquity identity**.
5. Génère secrets PostgreSQL sous `/etc/rackloom/secrets/` (0400).
6. TLS : certificat admin fourni ou CA locale auto-signée.
7. Charge les images Docker embarquées ; démarre Compose (postgres → init → migrate → services).
8. Affiche une fois l’URL + jeton d’amorçage (jamais stocké en clair sur disque).

Aucun secret n’est lu depuis l’ISO. Voir ADR-0004 §6 et `docs/security-model.md`.
