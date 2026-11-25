# ADR-0003 : isolation des runners

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Les jobs exécutent Terraform et Ansible avec des identifiants puissants et parlent à des systèmes non fiables (invités). Exigences : runners éphémères isolés par job, pas de conteneur privilégié, pas de socket Docker accessible à la WebUI ou à l'API, espaces de travail jetables, limites, sorties filtrées, droits réseau distincts par type de job.

## Décision

1. **Un service `launcher` dédié** est le seul à parler au moteur de conteneurs. Il n'a ni réseau ni accès à la base. Il expose au seul worker une API HTTP sur socket Unix (`/run/rackloom/launcher.sock`, 0660) : créer, inspecter, signaler, supprimer un run.
2. **Le launcher décide, le worker demande** : à partir du type de job, le launcher fixe l'image (liste blanche par empreinte), les montages (espace de travail du job ; pour Terraform, dossiers de la stack), le profil réseau, les limites et la durée maximale. Le worker ne peut que réduire ces valeurs. Toute autre option est refusée.
3. **Conteneur runner** : utilisateur 10001, `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, seccomp par défaut, `--pids-limit 256`, CPU et mémoire bornés, tmpfs `/tmp` et `/run/secrets`, étiquettes `rackloom.job_id` et `rackloom.correlation_id`. Jamais `privileged`, `host` ou socket monté.
4. **Réseau par type de job** : `none` pour Terraform en démo ; `rn-lab` (interne) pour Ansible et la vérification au lot 1 ; au lot 2, réseaux dédiés vers vCenter, le backend de state ou les réseaux de gestion, avec filtrage sortant nftables sur l'hôte limité aux endpoints approuvés.
5. **Secrets** : transmis sur l'entrée standard au démarrage, écrits en tmpfs par le point d'entrée, jamais en argument, variable d'environnement ou fichier sur disque.
6. **Sorties** : événements JSON bornés émis par le point d'entrée ; sortie brute dans un fichier de l'espace de travail, filtrée par le worker puis chiffrée. Espace de travail supprimé après collecte, conservé seulement en état incertain jusqu'à réconciliation.
7. **Moteur** : au lot 1, le moteur Docker du poste de développement (le launcher en monte le socket : risque documenté). Au lot 3, sur l'appliance, **Podman sans root** sous un utilisateur système `rackloom-runner`, via son API compatible Docker, pour que l'accès au moteur ne vaille plus root.
8. **Reprise** : le launcher conserve les runs terminés jusqu'à acquittement ; après redémarrage du worker, la réconciliation retrouve un run par `job_id`.

## Conséquences

- Un seul composant concentre le pouvoir de lancer des conteneurs ; il doit rester petit, sans dépendances lourdes, testé pour chaque refus.
- Le worker reste dans la base de confiance : il choisit quel job lancer (voir `docs/threat-model.md` R9).
- Champ `zone` présent dans l'API (valeur `local`), prêt pour des runners par site (F16).

## Alternatives écartées

- Socket Docker monté dans le worker : un worker compromis devient root sur l'hôte et accède aussi à la base.
- Sous-processus dans le conteneur worker (bubblewrap, nsjail) : exige des privilèges supplémentaires, isolation réseau par job plus difficile.
- Kubernetes Jobs : hors cible v1.
- gVisor ou microVM (Firecracker, Kata) : isolation plus forte, à évaluer si le modèle de menace l'exige, coût d'exploitation plus élevé.

## À trancher

- Confirmer Podman sans root pour l'appliance, ou retenir un autre moteur (Docker rootless, gVisor).
