# Modèle de menaces de Rackloom

Statut : lot 0. Les frontières de confiance TB1 à TB10 sont définies dans `docs/architecture.md` §4. La colonne « Vérification » renvoie aux tests (`T..`), aux règles de validation (`V..`) ou aux étapes (`E..`) de `docs/lot-1-plan.md`, ou au lot qui traitera le point.

## 1. Biens à protéger

1. **Capacité d'écriture sur le parc** (créer ou modifier des VM, configurer des OS) : c'est le bien principal ; un attaquant qui déclenche une écriture non approuvée a gagné.
2. **Intégrité de la décision technique** : les valeurs résolues (placement, réseau, image, dimensionnement, composants) doivent venir du résolveur, des templates publiés et des politiques, jamais d'un demandeur ou du modèle.
3. **Intégrité du registre** : contenu des versions publiées de templates et de composants, et droit de publier.
4. Identifiants des connecteurs (vCenter, backend de state, SSH, sudo) et clés de chiffrement de l'appliance.
5. State Terraform et plans binaires (ils contiennent des attributs sensibles).
6. Intégrité des approbations, de l'audit et de l'historique des changements.
7. Confidentialité entre projets : inventaire, journaux, artefacts, événements, motifs de blocage.
8. Disponibilité de l'appliance et exactitude de l'état affiché.

## 2. Acteurs

| Acteur | Hypothèse |
|---|---|
| Utilisateur authentifié malveillant ou curieux | cherche à sortir de son projet, à s'auto-approuver, à imposer une valeur technique (réseau, image, dimension, rôle), à injecter du HCL, des paramètres Ansible ou une destination |
| Client automatisé (CI, script) | mêmes droits que l'identité qui l'utilise ; peut rejouer ou forger des requêtes |
| Publicateur de templates | de confiance pour publier une version revue, pas pour déployer ni approuver sans rôle projet, ni pour modifier une version publiée |
| Auteur de templates (AT01) | de confiance pour proposer, pas pour publier son propre brouillon |
| Administrateur de plateforme | de confiance pour la configuration, pas pour lire les secrets existants ni les données projet sans rôle |
| Invité compromis | contrôle les faits, journaux, noms et sorties qu'il renvoie, et le serveur SSH en face du runner |
| Contenu non fiable relayé au modèle | journaux, noms de VM, documents, résultats d'outils contenant des instructions |
| Modèle | non fiable : peut halluciner, obéir à une injection ou produire une sortie invalide |
| Attaquant réseau sur le réseau d'administration ou de gestion | interception, usurpation de vCenter ou d'un hôte |
| Chaîne d'approvisionnement | image, paquet, provider, collection, rôle Ansible, contrat de composant ou modèle altérés |

## 3. Menaces et mesures

### 3.1 Besoin, résolveur et templates

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| B1 | Un demandeur impose une valeur technique (réseau, VLAN, image, vCPU, datastore, rôle, version) par l'UI, l'API, le chat, la CI, une duplication ou une reprise | un seul schéma de besoin (`extra="forbid"`) ; liste de clés techniques refusées à toute profondeur, même avec la valeur résolue ; aucun mode expert ; toutes les sources passent par le même résolveur ; table des routes vérifiée | V01, V02, T21, T32, T38 |
| B2 | Bascule silencieuse vers la production, un autre réseau ou un autre compte quand la cible autorisée est indisponible | production jamais par défaut ; `confirm_production` seulement d'un geste explicite ; aucun repli dans le résolveur ; blocage expliqué | V06, T33, T34 |
| B3 | Oracle d'inventaire : un motif de blocage ou une valeur résolue révèle des ressources d'un autre projet | codes de blocage génériques ; résolution limitée aux octrois du projet ; « Détails du plan » ne montre que la révision du changement, avec dépôt scopé | T09, T33 |
| B4 | Résolution recalculée entre approbation et exécution, ou différente pour des entrées identiques | fonction pure ; révision immuable avec `resolution_digest` ; l'exécution ne lit que la révision ; règle d'admission 55 qui vérifie sans recalculer | T35, T37, T40 |
| B5 | Une nouvelle version de template modifie un plan approuvé ou un déploiement existant | empreintes du template et des composants dans le dossier d'exécution ; déploiement épinglé sur sa version ; publication sans job ni écriture sur un déploiement | T35, T39 |
| B6 | Modification d'une version publiée (fichier de `catalog/` réécrit, mise à jour en base) | empreinte comparée au chargement et refus ; triggers d'immutabilité ; toute modification exige une nouvelle version | E3, T21 |
| B7 | Publication abusive : un opérateur ou un approbateur publie, ou un publicateur publie une version qui élargit des cibles ou un composant sans revue | rôle `template_publisher` distinct (un seul siège) ; motif obligatoire et aperçu d'impact ; audit ; versions `validated` chargées depuis `catalog/` revu, ou versions `in_review` de l'atelier publiées seulement par une personne autre que l'auteur qui les a soumises, sur l'empreinte affichée ; chaque déploiement reste soumis à son approbation et à l'admission | T39 ; `test_studio.py` |
| B8 | Secret littéral dans un template ou un composant (mot de passe, community SNMP, clé de licence) | contrats acceptant seulement des références de secrets ; chargeur qui refuse les champs secrets sous forme de valeur et les motifs connus ; canari | E3, T24 |
| B9 | Composant ou rôle Ansible malveillant introduit dans le registre | revue Git de `catalog/`, rôles épinglés par empreinte, effets et contrôles déclarés, runner isolé ; `verification_level` affiché (« non testé ») ; AT01 ajoute test sur machine jetable et revue avant publication | E3, E10 ; AT01 |
| B10 | Version retirée encore exécutée | règle d'admission 55 ; le retrait bloque sans modifier le plan | T35 |
| B11 | Épuisement des noms (réservations abusives) | réservation seulement à la soumission, dans le projet ; libération quand un changement se termine sans ressource ; débit limité | E4 |
| B12 | Champ inconnu transformé en argument libre, script ou YAML exécuté sur le parc | aucun paramètre hors contrat (V01) ; variables Ansible générées depuis la configuration déclarée des composants ; AT01 : extension proposée sous forme de patch à revoir | T21 ; AT01 |

### 3.2 Assistant et modèle

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| C1 | Injection indirecte (journal, nom de VM, document) qui pousse le modèle à proposer une action | aucune capacité d'action dans le schéma de sortie ; contenu non fiable délimité et marqué comme donnée ; revalidation serveur | T18 |
| C2 | Approbation conversationnelle (« oui, fais-le ») | aucune route d'approbation depuis le chat ; l'approbation exige session, CSRF et l'écran dédié | T06 |
| C3 | Le modèle propose une valeur technique, la production ou un service non octroyé | liste fermée de chemins modifiables par le modèle ; `production_requested` ouvre seulement le dialogue de confirmation ; suggestions revalidées par V01 à V10 ; aucune donnée d'inventaire technique dans le contexte | V02, V06, V07, T16, T32, T34 |
| C4 | Fuite de données d'un autre projet ou de secrets vers le modèle | contexte construit par des lectures scopées et limité aux services et au besoin ; module `chat` sans accès à `inventory`, `resolver` ni `secrets` (`import-linter`) ; filtrage des journaux avant envoi | T24, contrat `import-linter` |
| C5 | Exfiltration vers un endpoint de modèle distant | endpoint dans `approved_endpoints`, TLS avec CA validée, aucun repli cloud ; avertissement à l'admin que l'endpoint distant reçoit les données filtrées | E11 |
| C6 | Sortie du modèle rendue en HTML (XSS) | rendu texte uniquement, CSP stricte | T30 (axe et contrôle CSP) |
| C7 | Déni de service (modèle lent, prompts énormes) | délai 30 s, taille d'entrée et de sortie bornées, débit limité par utilisateur ; le formulaire reste utilisable | T17, T36 |

### 3.3 Runners et exécution

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| R1 | Évasion de conteneur depuis un runner | utilisateur non root, aucune capacité, `no-new-privileges`, seccomp, rootfs en lecture seule ; lot 3 : Podman sans root, donc évasion limitée à un utilisateur sans privilège | tests de refus du launcher (E5) ; lot 3 |
| R2 | Injection via paramètres (HCL, provider, provisioner, `ansible_*`, destination) | aucun paramètre technique venant du demandeur ; `tfvars` JSON généré depuis la révision ; variables Ansible générées depuis les composants ; `ansible.cfg` et inventaire générés et figés | T21, T32 |
| R3 | Module, provider ou rôle malveillant | catalogue épinglé par empreinte, lockfile avec hachages, miroir local de providers, aucune installation directe, aucun réseau pour le runner de démo | E6 |
| R4 | Contamination entre jobs | espace de travail neuf par job, montages limités à la stack du job, conteneur supprimé après collecte | E5 |
| R5 | Épuisement de ressources | limites CPU, mémoire, PIDs, durée ; sorties bornées | E5 |
| R6 | Plan modifié entre approbation et application | SHA-256 du plan dans l'empreinte, vérifié avant `apply` ; seul le fichier approuvé est appliqué | T01 |
| R7 | Plan périmé appliqué ou recalculé en silence | expiration, recalcul de l'empreinte à l'admission, refus du plan périmé par Terraform, aucun `plan` avant `apply` | T02, T03, T04 |
| R8 | Double écriture après panne | `dispatched_at`, état incertain, réconciliation par le launcher et le state avant toute relance, relance humaine | T13, T14, T15 |
| R9 | Worker compromis qui lance un `apply` sans admission | worker non exposé au réseau, rôle base limité, audit de chaque lancement par le launcher. **Risque résiduel** : le worker fait partie de la base de confiance d'exécution. Piste : tickets d'exécution signés par l'API et vérifiés par le launcher, à étudier avec F16 | ouvert |
| R10 | Launcher détourné (socket du moteur de conteneurs) | service sans réseau, API étroite, liste blanche d'images par empreinte, profils figés, socket Unix 0660 partagé avec le seul worker. **Risque résiduel au lot 1** : le socket Docker du poste équivaut à root ; lot 3 : Podman sans root | E5, ADR-0003 |
| R11 | Annulation présentée comme garantie | annulation = demande, état réel affiché, réconciliation | T23 |
| R12 | Suppression ou remplacement involontaire (par exemple un changement de niveau) | bloqués en v1 au plan, à l'approbation et à l'admission ; une stack par déploiement | T26, T27 |

### 3.4 Secrets et clés

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| S1 | Secret dans les journaux, événements, audit, SSE, plans résumés | filtrage des valeurs connues du job et de motifs ; résumés de plan par liste blanche et masquage des valeurs sensibles ; `no_log` imposé sur les tâches Ansible à secret ; canari | T24 |
| S2 | API compromise lisant les secrets | boîte scellée : l'API ne détient que la clé publique ; seul le worker déchiffre, au moment du job | E1, ADR-0007 |
| S3 | Clé stockée avec les données | clés hors base, montées en lecture seule là où nécessaire, sauvegarde séparée | ADR-0007, lot 3 |
| S4 | Runner recevant trop d'identifiants ou trop longtemps | identifiants du seul job, transmis sur l'entrée standard, tmpfs, supprimés à la fin ; lot 2 : identités d'inventaire et de mutation séparées ; F15 : certificats SSH courts | E10 |
| S5 | Secret visible via arguments ou variables d'environnement (`docker inspect`, `ps`) | interdit par le contrat du launcher ; test d'inspection du conteneur | E10 |
| S6 | State Terraform exposé | jamais en base, ni dans le chat, les événements ou les journaux ; `tf_state_read` ne renvoie qu'adresses, `lineage` et `serial` ; backend réel chiffré | E9, ADR-0002 |
| S7 | Bundle de support ou export contenant des secrets | exports générés depuis des données déjà filtrées, scan du canari sur l'export | lot 3, F22 |

### 3.5 Connecteurs

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| K1 | Usurpation de vCenter (MITM) | validation TLS avec CA importée et nom d'hôte, aucune option pour l'ignorer | T19 |
| K2 | Connecteur utilisé comme relais vers une URL ou un hôte arbitraire (SSRF) | endpoints définis par l'admin uniquement, références par identifiant d'inventaire résolues par le serveur, aucune URL fournie par l'utilisateur ou le modèle ; lot 2 : filtrage sortant nftables | E2, lot 2 |
| K3 | Compte vCenter trop privilégié | matrice de privilèges minimaux documentée au lot 2, identités d'inventaire et de mutation séparées | lot 2 |
| K4 | Écriture concurrente vCenter et Terraform | aucune action native vCenter sur une propriété gérée par Terraform ; dérive détectée en lecture (F09) | lot 2, F09 |
| K5 | Connecteur simulé utilisé en production | refus au démarrage en profil `production`, règle d'admission 10, badge « Simulé » | E2 |
| K6 | Capacité annoncée mais absente | matrice de capacités explicite, « non disponible » affiché, tests de contrat | E2, F30 |
| K7 | Résolution sur un inventaire périmé | fraîcheur enregistrée dans la révision ; instantané de plus de 24 h bloquant (`inventory_stale`) | T33 |

### 3.6 Invités

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| G1 | Usurpation d'hôte au premier contact | ancre de clé d'hôte (labo : import des clés ; lot 2 : clé publiée par la personnalisation invitée ; F15 : certificats d'hôte), `StrictHostKeyChecking=yes` | T20 |
| G2 | Données collectées piégées (injection, XSS, taille) | données non fiables, bornées (lignes, octets), filtrées, rendues en texte | T18 |
| G3 | Invité compromis attaquant le runner Ansible | runner éphémère, isolé, sans identifiant hors du job, sans réseau hors des cibles ; transfert d'agent SSH désactivé | E10 |
| G4 | Vol des identifiants de configuration sur l'invité | comptes de collecte et de configuration séparés, élévation limitée aux jobs de configuration ; F15 : certificats courts. **Limite** : Ansible requiert un sudo général pour `become` | E10, F15 |
| G5 | Paquet malveillant installé | paquets déclarés par les composants du registre uniquement, depuis des dépôts signés | T21 |
| G6 | Mouvement latéral d'un invité vers l'appliance | aucun flux entrant depuis les invités en v1 ; réseaux runners internes | T28 |

### 3.7 WebUI, API, sessions

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| W1 | Accès à un autre projet par identifiant (IDOR), y compris SSE, révisions et téléchargements | dépôts scopés, 404 hors périmètre, mêmes contrôles sur SSE et artefacts | T09 à T12 |
| W2 | CSRF | cookie SameSite=Strict, jeton CSRF, contrôle d'`Origin` | T29 |
| W3 | Vol ou fixation de session | cookie `__Host-` HttpOnly Secure, rotation à la connexion et au changement de privilège, expiration d'inactivité et absolue, révocation | E1 |
| W4 | Force brute | argon2id, limitation de débit, verrouillage temporaire | E1 |
| W5 | Auto-approbation | séparation des rôles côté serveur à l'approbation et à l'admission | T08 |
| W6 | Rejeu d'une écriture (y compris une soumission répétée par la CI) | `Idempotency-Key`, approbation à usage unique, contrainte d'unicité de l'exécution | T05 |
| W7 | Falsification de l'audit | ajout seul (droits et trigger), chaîne de hachage, `rackloom audit verify` | E1 |
| W8 | Jeton d'amorçage intercepté | affiché sur la console seulement, haché en base, 30 min, usage unique | E1 |
| W9 | Champ éditable caché dans « Détails du plan » ou contournement de l'UI | panneau sans contrôle de saisie (vérifié dans le DOM) ; le serveur refuse de toute façon | T31, T32 |

### 3.8 Appliance et exploitation

| ID | Menace | Mesures | Vérification |
|---|---|---|---|
| A1 | Service interne exposé | seul 443 publié, PostgreSQL, Ollama, launcher et runners sur réseaux internes | T28 |
| A2 | ISO ou dépendance altérée | image amont vérifiée (SHA256SUMS signé), dépendances épinglées, SBOM, manifeste | lot 3 |
| A3 | Secret ou mot de passe embarqué dans l'ISO | aucun ; secrets générés au premier boot | lot 3 |
| A4 | Restauration qui relance d'anciens jobs | démarrage restauré avec envois suspendus, réconciliation avant reprise | lot 3, F26 |
| A5 | Appliance hébergée sur le vCenter qu'elle administre | procédure de récupération depuis une nouvelle appliance, sauvegardes hors du vCenter | lot 3 |
| A6 | Mise à jour qui modifie le parc | une mise à jour ne crée aucun job ; elle invalide les approbations en attente si les images runner changent | lot 3 |

### 3.9 Atelier de conception (AT01 et suivants, hors lot 1)

Première tranche AT01 : ces mesures sont en place pour le brouillon, la revue et les vagues. La double validation (un relecteur en plus du publicateur) reste une proposition, ADR-0010, non implémentée.

| ID | Menace | Mesures prévues |
|---|---|---|
| AC1 | Le modèle publie, obtient un secret ou exécute une commande | aucune capacité de publication, de lecture de secret ni d'exécution dans le schéma de l'atelier ; publication par un rôle distinct après revue |
| AC2 | Secret saisi dans le chat de conception ou écrit dans un brouillon | références au coffre seulement ; détection et refus des valeurs secrètes dans le brouillon et le contexte ; canari |
| AC3 | Patch d'extension (schéma, rôle) intégré sans revue | patch soumis à la revue Git, à la CI et à un test sur machine jetable avant d'entrer dans `catalog/` |
| AC4 | Application par vagues qui inclut une cible ajoutée après approbation | liste de cibles figée dans le dossier d'exécution ; une nouvelle cible exige une nouvelle approbation |
| AC5 | Template déclaré « validé pour la production » sur la foi d'une conversation ou d'un faux connecteur | `verification_level` calculé depuis les preuves enregistrées, jamais déclaré |

### 3.10 Fonctions utilisateur F51 à F60 (hors lot 1)

Mêmes principes : chaque lecture, recherche, lien, sortie de job et action passe par les contrôles serveur existants (dépôts scopés, `authz.require`, `workflow.available_actions`). Les règles d'architecture correspondantes sont dans `docs/architecture.md` §12.1.

| ID | Menace | Mesures prévues | Fonction |
|---|---|---|---|
| U1 | Une demande inaccessible est révélée par la détection de doublons : titre, extrait, nombre de résultats, message différent (« 1 demande similaire existe, vous n'y avez pas accès »), temps de réponse | recherche exécutée uniquement dans le dépôt scopé de l'utilisateur, avant tout calcul de similarité ; compteurs et messages calculés sur ce seul ensemble ; aucune mention de demandes hors périmètre, même anonymisée ; même réponse qu'il existe ou non une demande inaccessible ; test avec une demande similaire dans un projet non autorisé | F53 |
| U2 | Le partage d'une demande ou d'un élément de plan sert à obtenir un accès | partager ne crée aucun `role_binding` ; seul un collègue déjà habilité sur le projet peut être sollicité et lire ; un lien partagé ouvert par une personne non habilitée renvoie 404 ; un commentaire ne modifie pas le plan et ne vaut pas approbation ; une nouvelle version qui change la cible ou le contenu approuvé repart en validation | F54 |
| U3 | Collecteur détourné pour exécuter une commande, lire un fichier arbitraire ou exfiltrer un secret depuis le serveur de référence | collecteurs approuvés, versionnés et exécutés par le runner avec l'identité de collecte sans élévation ; champs sur liste blanche, tout le reste ignoré ; aucune commande composée par l'IA ni paramètre libre ; sorties bornées, filtrées (valeurs secrètes connues, motifs) et traitées comme données non fiables ; hôte enrôlé avec clé d'hôte ancrée et droit d'observation de l'auteur vérifié | F60 |
| U4 | Identifiants de l'hôte de référence ou secrets recopiés dans un brouillon puis publiés | classement « Propre à cet hôte » pour IP, nom, UUID de volumes et identifiants, jamais recopiés ; scan du brouillon et du contexte du modèle avec un canari ; contrôle du chargeur au moment de la validation (B8) | F60 |
| U5 | Repartitionnement ou modification de l'hôte de référence pendant l'analyse | collecte en lecture seule, aucun job d'écriture créé par l'atelier ; repartitionnement implicite refusé, renvoi vers une image compatible ou une installation automatisée ; test : empreinte de l'hôte identique avant et après la collecte | F60 |
| U6 | Faux « prêt » : la fiche service affiche un succès fondé sur le seul code de sortie d'un job ou sur un résumé du modèle | état du service calculé depuis l'état observé et les contrôles de santé du template, avec preuve et heure ; aucune entrée du modèle | F57 (base : T41) |
| U7 | Action proposée ou exécutée sans droit (bouton visible, appel API direct) | actions calculées côté serveur et revérifiées à l'appel ; un bouton de la fiche ouvre un changement prérempli soumis au résolveur, au plan, à l'approbation et à l'admission | F55, F58 |
| U8 | Élévation par demande d'accès : groupe privilégié saisi par le demandeur, « accès accordé » affiché sans attribution réelle, attribution en double après relance | groupe résolu par correspondance approuvée et versionnée, jamais saisi ; éligibilité et approbation vérifiées ; état « accordé » seulement après confirmation de l'IdP ; relance idempotente ; aucun mot de passe affiché | F59 |
| U9 | Exemples ou comparateur qui révèlent des services, environnements ou capacités non autorisés, ou affichent un prix non vérifié | exemples construits depuis les templates publiés octroyés et les droits réels (aucune suggestion de production sans droit) ; comparateur limité aux options autorisées et aux données vérifiées | F51, F52 |
| U10 | Estimation de délai présentée comme une garantie ou inventée par le modèle | fourchettes calculées sur des exécutions comparables avec historique suffisant, sinon « délai non estimable » ; aucun appel au modèle pour estimer | F56 |

## 4. Risques résiduels acceptés au lot 1

- R9 et R10 : le worker et le launcher font partie de la base de confiance d'exécution ; le socket Docker du poste de développement donne au launcher l'équivalent de root.
- B7 et B9 : la confiance dans le registre repose sur la revue Git de `catalog/` et sur le rôle de publication ; les versions ne sont pas signées au lot 1 (signature à étudier avec AT01 et F17).
- G4 : élévation Ansible large sur les invités de labo.
- Comptes locaux sans second facteur (OIDC et réauthentification forte au lot 4 et avec F12).
- Pas de filtrage sortant au niveau de l'hôte avant le lot 2 (les runners de démo n'ont pas de réseau, ceux d'Ansible seulement `rn-lab`).
- La soumission par CI est simulée avec une identité utilisateur ; les identités de service limitées arrivent avec F35.
