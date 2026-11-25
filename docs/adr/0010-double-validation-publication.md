# ADR-0010 : double validation avant publication d'un template

- Statut : proposé (AT01, 2026-11-25). Non implémenté.
- Décideur attendu : Vincent

## Contexte

D16, tranchée pour le lot 1 : une seule personne tient le rôle `template_publisher`. La double validation devait être étudiée avec AT01, pas livrée au lot 1.

AT01 ajoute un auteur (`template_author`) qui soumet une version `in_review`. Le publicateur unique la publie, ou la renvoie, sur l'empreinte affichée. L'auteur qui a soumis la version ne peut pas la publier lui-même. Cela sépare concevoir et publier. Ce n'est pas une double validation : une seule personne décide encore de la publication.

## Décision proposée

Garder le siège unique de publicateur. Ne pas ajouter un troisième rôle sans décision de Vincent.

Si la double validation est retenue plus tard, le cycle serait :

1. `in_review` : l'auteur soumet une version immuable, empreinte calculée sur le contrat.
2. `accepted_by_reviewer` : un relecteur, distinct de l'auteur et du publicateur, accepte cette empreinte. Il ne publie pas et ne déploie pas.
3. `published` : le publicateur unique publie la même empreinte. Il n'est ni l'auteur ni le relecteur.

Chaque geste est authentifié, audité, et refusé si l'empreinte affichée n'est plus celle de la version. Un renvoi ramène le brouillon en édition et produit une nouvelle version, comme aujourd'hui. Publier ne crée toujours aucun job et ne modifie ni un plan approuvé ni un déploiement existant.

## Conséquences

- Tant que cet ADR n'est pas accepté, le comportement livré reste : un auteur, un publicateur distinct, empreinte exigée pour `in_review`. Les versions `validated` chargées depuis `catalog/` se publient comme au lot 1, sans relecteur.
- Un troisième rôle augmenterait le nombre de comptes de démonstration et les cas de séparation à tester. Il n'est pas nécessaire pour les critères d'AT01.

## Alternatives écartées

- Laisser l'auteur publier sa propre version : contredit la séparation déjà en place pour `in_review`.
- Exiger deux publicateurs : contredit D16 (un seul siège) et complique le départage si les deux refusent.
- Faire du modèle le relecteur : une suggestion de l'assistant n'est ni une revue ni une publication.
