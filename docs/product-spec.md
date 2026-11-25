# Spécification produit de Rackloom

Statut : lot 0. Décrit le produit v1 ; le lot 1 en réalise le parcours principal (voir `docs/lot-1-plan.md`).

## 1. Promesse

Un utilisateur autorisé décrit **ce dont il a besoin** (« une VM Ubuntu de test avec Nginx ») et obtient, sans compte direct dans vCenter, Terraform ou Ansible, des machines conformes à un service publié, configurées, vérifiées et suivies, avec une approbation explicite et une trace complète.

**L'utilisateur choisit le besoin, Rackloom choisit la technique.** Le demandeur ne choisit ni vCPU, ni RAM, ni réseau, ni datastore, ni template vSphere, ni runner : Rackloom les résout depuis des **templates Rackloom** publiés et versionnés, les politiques de l'environnement et l'inventaire autorisé, puis les montre en lecture seule. L'appliance orchestre les outils existants ; elle ne les remplace pas.

## 2. Profils et droits

Trois droits sont distincts : **déployer** un service publié, **concevoir** un template, **publier** une version de template. S'y ajoutent l'approbation et l'audit.

| Profil | Peut | Ne peut pas |
|---|---|---|
| Opérateur (déployer) | sur ses projets : décrire un besoin, utiliser l'assistant, soumettre, consulter plans, jobs, machines et journaux autorisés, modifier un déploiement, demander une annulation ou une reprise | fixer une valeur technique ; approuver ; publier ; accéder à un autre projet |
| Approbateur | sur ses projets : examiner un plan et l'approuver ou le refuser, approuver une reprise | approuver sa propre demande là où l'environnement l'interdit (toujours en production) ; modifier ou publier un template |
| Publicateur de templates (publier) | publier, déprécier ou retirer une version de template validée, avec motif et aperçu d'impact | déployer ou approuver sans rôle sur le projet ; modifier le contenu d'une version publiée |
| Auteur de templates (concevoir) | **avec AT01** : créer et tester des brouillons de templates dans l'atelier | publier son propre brouillon ; déployer en production depuis l'atelier |
| Administrateur de plateforme | gérer utilisateurs et rôles, connecteurs et leurs secrets (écriture seule), endpoints approuvés, politiques d'environnement, octrois, santé de l'appliance | lire les données d'un projet sans rôle sur ce projet ; lire un secret existant |
| Lecteur / auditeur | consulter demandes, révisions, plans, jobs, machines, historique des templates et audit de son périmètre | toute écriture |
| Identités de service | `worker`, `launcher`, comptes techniques des connecteurs (lot 1) ; identités de CI avec F35 : seulement ce que leur rôle exige | se connecter à l'interface |

Un utilisateur peut cumuler des rôles. Les droits sont attribués par projet et éventuellement par environnement, et contrôlés côté serveur sur chaque ressource. Il n'existe pas de « mode expert » : aucun rôle ne peut fixer une valeur technique dans une demande. Un besoin technique particulier passe par une nouvelle version de template, conçue puis publiée.

## 3. Les trois catégories de données

| Catégorie | Qui | Où |
|---|---|---|
| **Besoin** : projet (s'il y en a plusieurs), service, environnement (seulement s'il existe un choix ou pour demander la production), quantité, niveau de service, usage, date de fin | le demandeur | parcours « Déployer » |
| **Résolu et verrouillé** : image vSphere, placement, datastore, réseau et VLAN, adressage, nom, vCPU, RAM, disque, versions des composants et des rôles, module, runner, backend de state, identités techniques, sauvegarde, quotas, politiques, circuit d'approbation | Rackloom | panneau « Détails du plan », lecture seule |
| **Administration** : templates et composants, politiques d'environnement, octrois, connecteurs | rôles habilités | section « Administration » |

Une question n'est posée que si Rackloom ne connaît pas la réponse et si elle change un résultat visible (site, délai, disponibilité, budget, date de fin exigée). Un champ qui n'a qu'une valeur possible n'est pas affiché. La production n'est jamais choisie par défaut. Si aucune cible autorisée n'existe, Rackloom bloque et explique, sans jamais basculer vers un autre environnement, réseau ou compte.

## 4. Parcours

### Déployer

**P1. Décrire le besoin.** L'utilisateur choisit un service (par exemple « Serveur web Ubuntu ») ou le décrit en une phrase dans le chat. Le formulaire, généré depuis le contrat du template, ne montre que les entrées fonctionnelles manquantes, chacune avec sa provenance : saisie, défaut du service, défaut de l'environnement ou suggestion à confirmer. L'environnement hors production est déduit s'il est unique. La production se demande par un lien dédié, avec une confirmation explicite.

**P2. Vérifier et soumettre.** Un récapitulatif en langage clair indique ce qui sera créé ou modifié, où, avec quel niveau de service, quelles vérifications et quelle approbation. « Risques et limites » ne masque jamais une suppression, un remplacement, la production ou une approbation requise. Le panneau **« Détails du plan »** montre en lecture seule chaque valeur résolue avec sa provenance, la fraîcheur de l'inventaire et les étapes simulées ; une fois le plan calculé, il présente aussi le diff Terraform et le contrat Ansible. Si la résolution est impossible, le motif est affiché avec le référent du projet, sans question technique.

**P3. Suivre le résultat.** Chronologie en direct (statuts lisibles, sources, heures, étapes simulées), reprise automatique après déconnexion, annulation présentée comme une demande, résultat par machine, puis lien vers les fiches machine.

**P4. Approuver.** L'approbateur voit le même récapitulatif et le même panneau, plus le diff, l'empreinte et les versions (template, composants, module, rôles, images). Il approuve ou refuse avec un commentaire. L'approbation est liée à la révision et à l'empreinte : toute modification la rend caduque.

**P5. Vérifier le service.** Le déploiement affiche l'état de son service (prêt, créé mais pas prêt, échec, vérification indisponible) avec la preuve et l'heure de chaque contrôle de santé. Chaque fiche machine indique l'OS, la disponibilité observée, les services suivis, les paquets et leurs versions, la dernière collecte, l'état du dernier déploiement, la version de template et la provenance de chaque information (vCenter, stack Terraform, hôte vérifié).

**P6. Modifier un déploiement.** « Modifier ce déploiement » ouvre le même parcours sur les entrées modifiables (quantité, niveau). La version de template reste celle du déploiement ; passer à une version plus récente sera un changement distinct, proposé avec F19 et l'atelier (AT01). Un remplacement ou une suppression est bloqué et affiché.

**P7. Reprendre après un échec partiel.** « VM créée, configuration incomplète » : l'utilisateur demande une reprise ciblée de l'étape en échec, soumise à approbation. Rien n'est supprimé.

**P8. Utiliser le chat.** Le chat interprète la phrase, pose au plus deux questions utiles par tour et remplit le formulaire avec des suggestions à confirmer. Il ne propose jamais de valeur technique ni la production, et n'approuve ni ne lance rien. S'il est indisponible ou répond de façon invalide, un bandeau l'indique et le formulaire reste complet.

### Administrer

**P9. Publier un template.** Le publicateur ouvre « Administration › Templates » et voit la liste des templates et composants, leurs versions et statuts, le contrat rendu en lecture et l'historique. Il publie, déprécie ou retire une version, avec un motif et un aperçu des projets et déploiements concernés. Publier ne déploie rien et ne modifie aucun plan approuvé ni aucune ressource existante. Au lot 1, les versions sont écrites et revues dans le dépôt (`catalog/`) ; l'atelier de conception assisté arrive avec AT01.

**P10. Administrer la plateforme.** Amorçage à usage unique depuis la console, premier administrateur, connecteurs (configuration, secrets, CA, test de connexion, matrice de capacités), projets et référents, environnements et politiques versionnées, octrois de ressources, utilisateurs et rôles.

**P11. Auditer.** Filtrer l'audit par projet, changement, acteur ou action : demandeur, approbateur, cibles, versions (template, politique, révision), décisions d'admission, actions et résultats. Les publications de templates sont auditées avec leur motif.

## 5. Écrans

| Écran | Contenu | États à soigner |
|---|---|---|
| Connexion, amorçage | identifiants, jeton d'amorçage | jeton expiré ou utilisé, compte verrouillé |
| Accueil et santé | changements récents, actions en attente, santé (file, workers, connecteurs, assistant) | aucun projet, worker absent, jobs bloqués |
| Déployer, étape 1 « Décrire le besoin » | services du projet, chat et formulaire généré côte à côte, badges de provenance, lien « Demander un déploiement en production » | assistant indisponible, entrée invalide, aucun service disponible |
| Déployer, étape 2 « Vérifier et soumettre » | récapitulatif, « Risques et limites », « Détails du plan » en lecture seule | résolution impossible (motif et référent), inventaire ancien, version obsolète |
| Déployer, étape 3 « Suivre le résultat » | chronologie, étapes simulées, annulation, résultat par machine | flux interrompu, résultat incertain, annulation non honorée |
| Demande | versions du besoin, révisions, état, historique, plans, exécution | droits insuffisants |
| Plan et approbation | étape 2 complétée du diff, de l'empreinte et des motifs de blocage | plan expiré, bloqué (suppression ou remplacement), approbateur identique au demandeur |
| Déploiements, machines, détail machine | liste, version de template, fiche (P5), « Modifier ce déploiement » | non collecté, inaccessible, données anciennes |
| Administration › Templates | templates, composants, versions, statuts, contrats, historique, impact, actions de publication | aucun template publié, version retirée encore utilisée |
| Administration › Environnements | politiques versionnées, octrois | — |
| Administration › Inventaire | éléments autorisés avec fraîcheur | connecteur en erreur, données anciennes |
| Administration › Connecteurs | configuration, matrice de capacités (« non disponible », « simulé »), test | certificat refusé, endpoint non approuvé |
| Administration › Utilisateurs et rôles | comptes, rôles par projet, révocation | — |
| Audit | liste filtrable, détail | aucun résultat |

Le parcours « Déployer » n'affiche aucun champ vCPU, RAM, disque, réseau, VLAN, IP, datastore, cluster, image, template vSphere ou runner. L'interface est en français et prête pour l'anglais (toutes les chaînes dans les fichiers i18n). Elle est utilisable entièrement au clavier, avec des contrastes conformes WCAG 2.1 AA. Les textes issus des invités, des outils ou du modèle sont affichés comme du texte, jamais interprétés comme du HTML.

## 6. Statuts visibles

**Changement** : Brouillon, Précisions requises, Résolution impossible, Validée, Plan en cours, Échec du plan, Plan prêt, Plan expiré, Refusée, Approuvée, Bloquée à l'admission, Exécution, Vérification, Succès, Succès partiel, Échec, Résultat incertain, Réconciliation, Annulée. Chaque statut indique ce que l'utilisateur peut faire ensuite.

**Version de template** : validée, publiée, dépréciée (encore utilisable par les déploiements existants, plus proposée pour une création), retirée (plus aucune exécution). Les statuts brouillon, testée en labo et en revue arrivent avec AT01.

**Service d'un déploiement** : prêt, créé mais pas prêt, échec, vérification indisponible, non vérifié. Calculé depuis l'état observé et les contrôles de santé du template, jamais depuis le seul succès d'un job ni depuis une réponse du modèle. Une demande n'est en « Succès » que si le service est prêt ; la fiche « Votre service est prêt » arrive avec F57.

**Machine** : sain, en erreur, inaccessible, données anciennes, non collecté.

**Provenance d'une valeur** : saisie par l'utilisateur, défaut du service, défaut de l'environnement, suggestion non confirmée (besoin) ; template, politique d'environnement, inventaire, départage (valeurs résolues).

**Niveau de preuve d'une capacité ou d'un composant** : simulé, testé localement, validé en laboratoire réel, non vérifié.

## 7. Critères d'acceptation produit

Les démonstrations D1 à D6 sont les critères d'acceptation du lot 1 ; leur scénario détaillé est dans `docs/lot-1-plan.md` §1.2.

| ID | Critère | Vérifié par |
|---|---|---|
| D1 | « Je veux une VM Ubuntu de test avec Nginx » aboutit à un plan compréhensible avec au plus trois choix utilisateur et aucun champ d'infrastructure | T31 |
| D2 | Les réglages techniques sont visibles dans « Détails du plan » et non modifiables, ni par l'UI, ni par l'API, ni par le chat, ni par la CI | T32 |
| D3 | Sans réseau autorisé, la demande est bloquée avec un message clair qui oriente vers le référent du projet, sans question technique | T33 |
| D4 | Une demande sans environnement va en hors production, jamais en production ; un retrait de ressource bloque sans bascule ; la production exige une demande explicite | T34 |
| D5 | Une nouvelle version de template n'altère ni un plan approuvé ni un déploiement existant ; un retrait bloque l'exécution sans la modifier | T35 |
| D6 | Sans assistant, le formulaire suffit et applique les mêmes règles | T36 |
| CA-01 | Chaque valeur du besoin et chaque valeur résolue affiche sa provenance ; une suggestion non confirmée bloque la soumission sur les champs protégés | T31, T37 |
| CA-02 | Le serveur refuse tout champ technique ou hors contrat, toute valeur hors droits, par toutes les sources | T21, T32 |
| CA-03 | Mêmes entrées et même inventaire donnent la même résolution ; une duplication ou une reprise repasse par le résolveur et par une nouvelle approbation | T37, T38 |
| CA-04 | Déployer, approuver et publier sont des droits distincts ; publier ne crée aucun job | T39 |
| CA-05 | Une approbation n'est possible que par un approbateur autorisé, distinct du demandeur lorsque l'environnement l'exige, sur un plan prêt, non expiré, sans suppression ni remplacement | T04, T08, T26 |
| CA-06 | Une approbation ne sert qu'une fois et devient caduque si le besoin, la révision, le plan, les cibles, les versions ou les règles changent | T01 à T05, T40 |
| CA-07 | Un droit révoqué avant le démarrage bloque l'exécution avec un motif lisible | T07 |
| CA-08 | La chronologie est complète après reconnexion, sans doublon ni événement d'un autre projet | T12 |
| CA-09 | Une panne du worker n'entraîne ni doublon ni relance automatique d'une écriture au résultat inconnu | T13, T14, T15 |
| CA-10 | Un échec de configuration après création affiche « VM créée, configuration incomplète » et propose une reprise approuvée ; rien n'est supprimé | T22 |
| CA-11 | L'annulation est présentée comme une demande ; l'état affiché ensuite est l'état réel | T23 |
| CA-12 | Aucun secret n'apparaît dans l'UI, le chat, les templates, les journaux, les événements, l'audit ou les exports | T24 |
| CA-13 | Les étapes simulées sont étiquetées et refusées en profil production | T30 |
| CA-14 | Le modèle ne reçoit aucun inventaire technique, ne propose aucune valeur technique et ses réponses invalides sont ignorées | T16, T17, T18 |
| CA-15 | Seul HTTPS est exposé | T28 |
| CA-16 | Un job réussi n'est jamais présenté comme un service prêt : une sonde en échec donne « créé mais pas prêt », un hôte injoignable « vérification indisponible » | T41 |

## 8. Profil de démonstration

Organisation fictive, projets `vitrine` et `compta` avec leurs référents, identités `*.demo.invalid` aux mots de passe générés à l'installation, inventaire fictif, nœuds de labo en conteneurs, template `ubuntu-web` publié. Détail : `docs/lot-1-plan.md` §5. Le profil est refusé au démarrage en profil `production`.

## 9. Hors périmètre de la v1

- Mode expert ou surcharge d'une valeur technique dans une demande, quel que soit le rôle.
- Suppression, retrait ou remplacement de ressources (le retrait approuvé arrive avec F06).
- Actions natives vCenter modifiant une propriété gérée par Terraform.
- Terminal SSH ou commande libre vers un hôte, navigation libre dans le système de fichiers distant.
- Remédiation autonome, rollback automatique.
- Modules, providers, HCL, playbooks, rôles ou plugins fournis par l'utilisateur hors du registre revu.
- Repli vers un modèle cloud.
- Systèmes invités autres qu'Ubuntu et Debian.
- Kubernetes, haute disponibilité de l'appliance.
- Adaptateurs HCP Terraform, Terraform Enterprise, AWX/AAP : interfaces prévues, non implémentées.

Hors lot 1 mais prévu au backlog : atelier de conception de templates et application par vagues (AT01 à AT04), template depuis un serveur de référence (F60), fiche « Votre service est prêt » (F57), boîte « À moi d'agir » (F55), préférences de projet (F50), préflight (F31), identités de CI (F35).

## 10. Glossaire

- **Besoin** : ce que l'utilisateur demande, en termes fonctionnels (catégorie A). Chaque version est immuable.
- **Service, template Rackloom** : contrat versionné qui décrit comment Rackloom produit un service : méthode de création, profil système, composants, paramètres par environnement, références de secrets, contrôles de santé, politique de déploiement. Un « profil de service » est une version publiée d'un template. Les entrées F01 à F30 du backlog parlent de « blueprint » pour la même notion.
- **Composant** : brique logicielle versionnée (par exemple Nginx) avec un contrat de paramètres, ses dépendances, ses effets attendus et ses contrôles. Les formulaires de configuration sont générés depuis ce contrat.
- **Image vSphere** : point de départ d'une VM, inventorié dans vCenter. Ce n'est pas un template Rackloom.
- **Résolveur** : fonction unique côté serveur qui transforme un besoin en valeurs techniques, avec leur provenance.
- **Révision** : résultat enregistré d'une résolution (valeurs, provenance, versions, fraîcheur, empreinte de résolution). L'approbation porte sur une révision.
- **Changement** : unité suivie de bout en bout (création ou modification d'un déploiement) ; son identifiant est l'identifiant de corrélation.
- **Déploiement** : ensemble de machines créé à partir d'une version de template, avec sa stack Terraform.
- **Plan** : résultat d'un `terraform plan` sauvegardé, chiffré, résumé et daté.
- **Dossier d'exécution, empreinte** : ensemble canonique de ce qui sera exécuté (révision, versions, cibles, plan, rôles, images, règles) et son SHA-256.
- **Approbation** : décision liée à une empreinte, à usage unique.
- **Admission** : contrôle unique au démarrage effectif d'un job.
- **Job, runner** : unité de travail en file ; conteneur éphémère qui l'exécute.
- **Stack** : unité de state Terraform d'un déploiement.
- **Hôte enrôlé** : machine dont la clé d'hôte SSH a été vérifiée contre une ancre de confiance.
- **Référent du projet** : personne désignée vers qui orientent les messages de blocage.
