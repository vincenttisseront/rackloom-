# ADR-0008 : résolveur central, besoin et valeurs verrouillées

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Rackloom doit être utilisable par quelqu'un qui ne connaît ni vSphere, ni Terraform, ni Ansible. La règle produit est : **l'utilisateur choisit le besoin, Rackloom choisit la technique**. Les demandes arrivent par la WebUI, le chat, l'API, la CI, les duplications et les reprises. Si chaque canal appliquait ses propres défauts, ou si l'interface préremplissait des valeurs techniques, les règles divergeraient et une valeur technique pourrait être imposée par un canal moins contrôlé. L'approbation doit porter sur des valeurs précises, qu'on ne recalcule pas en silence avant l'exécution.

## Décision

1. **Trois catégories de données.**
   - A, le besoin : projet, service, environnement seulement s'il y a un choix ou une demande de production, entrées fonctionnelles déclarées par le template.
   - B, le résolu et verrouillé : image, placement, réseau, adressage, nom, dimensions, versions, rôles, runner, backend de state, identités, sauvegarde, quotas, politiques, circuit d'approbation.
   - C, l'administration : templates, politiques d'environnement, octrois, connecteurs.
   Seule la catégorie A entre dans une demande.
2. **Un seul schéma de besoin** (`rackloom.need/v1`, Pydantic `extra="forbid"`) pour toutes les sources. Tout champ hors schéma ou hors contrat d'entrées est refusé (422 `field_not_allowed`). Toute clé technique connue, à quelque profondeur que ce soit, est refusée (422 `locked_field`), **même si sa valeur égale la valeur résolue**. Il n'existe pas de mode expert.
3. **Un seul résolveur côté serveur** : fonction pure `resolve(need, principal, snapshot, now) -> Resolution` dans le module `resolver`, seul module autorisé à produire des valeurs de catégorie B (`import-linter`). Il ne fait aucun appel réseau, et les mêmes entrées avec le même instantané donnent la même sortie et la même empreinte (`resolution_digest`).
4. **Ordre de résolution** :
   1. contraintes obligatoires (plateforme, projet, environnement, template ; instantané d'inventaire trop ancien bloquant) ;
   2. environnement : celui demandé, sinon l'unique environnement hors production admissible ;
   3. version de template : dernière publiée pour une création, version épinglée pour une modification ;
   4. préférence de projet approuvée (F50, vide au lot 1) ;
   5. défaut du template ;
   6. départage déterministe (rang dans la liste du template, puis identifiant stable) ;
   7. blocage expliqué si une dimension n'a aucun candidat.
5. **Jamais la production par défaut, jamais de repli.** La production exige `confirm_production: true`, posé par un geste explicite de l'utilisateur ou de l'appelant API ; une suggestion du modèle ne le pose jamais. Une cible indisponible bloque ; on ne bascule pas vers un autre environnement, réseau ou compte.
6. **Questions minimales** : une question n'est posée que si Rackloom ne connaît pas la réponse et qu'elle change un résultat visible (site, délai, disponibilité, budget, date de fin exigée). Un champ à une seule valeur possible n'est pas affiché.
7. **Révision enregistrée** à chaque version du besoin (`request_revisions`, immuable), qu'elle soit résolue ou bloquée. Elle contient les valeurs résolues avec leur provenance, les versions du template et des politiques, la fraîcheur de l'inventaire et l'empreinte de résolution. La soumission exige une révision résolue et réserve les noms.
8. **Liaison à l'approbation** : la révision entre dans le dossier d'exécution (ADR-0006). L'exécution ne lit que la révision. À l'admission, la règle 55 vérifie que la résolution reste admissible (version non retirée, valeurs encore octroyées) ; si ce n'est pas le cas, le job est bloqué et une nouvelle révision explicite est exigée. On ne résout jamais à nouveau en silence.
9. **Duplications, reprises, CI** : une duplication crée un nouveau changement et une nouvelle résolution ; une reprise réutilise la même révision et revérifie les droits à l'admission ; la CI envoie le même besoin par la même route (identités de service avec F35).
10. **Motifs de blocage génériques** (codes stables, message en français et nom du référent du projet) qui ne révèlent aucune ressource hors du périmètre de l'utilisateur.
11. **Le modèle interprète, le serveur résout** : l'assistant ne reçoit ni inventaire technique ni valeur résolue sensible, et ne peut modifier que des chemins de catégorie A, sous forme de suggestions revalidées.

## Conséquences

- Le formulaire de l'étape 1 ne contient aucun champ d'infrastructure ; « Détails du plan » est en lecture seule.
- Un besoin technique particulier (taille hors catalogue, réseau dédié) passe par une nouvelle version de template, conçue puis publiée : c'est voulu.
- Le résolveur devient un composant critique : une dimension par sous-module, tests de déterminisme et de provenance, couverture de chaque code de blocage.
- La liste des clés de V02 n'a pas besoin d'être exhaustive pour être sûre : V01 refuse déjà tout champ hors schéma ; V02 donne un message plus clair.
- Les fonctions futures se branchent sans changer le contrat : préférence de projet (F50), placement et anti-affinité (F32), comparateur de choix métier (F52), capacité (F05), licences (F34).

## Alternatives écartées

- Défauts dans l'interface ou dans chaque canal : divergence assurée, et l'API contournerait l'UI.
- Mode expert réservé à certains rôles : réintroduit les valeurs techniques dans le parcours et rend la révision non reproductible.
- Accepter une valeur technique égale à la valeur résolue : rend le refus dépendant de l'état courant et fuit de l'information sur la résolution.
- Recalculer la résolution à l'exécution : contredit l'approbation.
- Moteur de règles générique (OPA, Rego) pour la résolution au lot 1 : prématuré ; les règles d'admission pourront l'utiliser avec F10.

## À trancher

- Règle de départage quand plusieurs sites existent (F32), et critères qui justifient une question à l'utilisateur.
- Durée de vie exigée en `dev` (30 jours au plus proposés) et rappel associé (F06).
- Qui est le référent affiché dans les messages de blocage : responsable du projet ou équipe plateforme.
