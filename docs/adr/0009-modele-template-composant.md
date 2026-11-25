# ADR-0009 : templates Rackloom, registre de composants, séparation avec les images vSphere

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Le résolveur (ADR-0008) a besoin de « profils de service » versionnés qui disent comment produire un service. Ces profils doivent :
- décrire un résultat complet et vérifiable, pas seulement une image de départ ;
- être immuables une fois publiés, pour qu'un plan approuvé reste reproductible ;
- s'étendre à de nouveaux logiciels sans écrire un écran ou un formulaire par logiciel ;
- être conçus par des auteurs habilités, plus tard avec l'aide de l'IA dans un atelier (AT01), et publiés par un rôle distinct.

Une confusion courante est de traiter un template vSphere (une VM modèle) comme le standard lui-même : le partitionnement, les logiciels et les contrôles en seraient implicites et invérifiables.

## Décision

1. **Profil de service = version publiée et immuable d'un template Rackloom.** Le contrat (`rackloom.template/v1`) déclare :
   - la méthode de création : `clone_image` (image vSphere existante, filtrée par attributs) au lot 1, `autoinstall` (installation Ubuntu automatisée) réservée à AT02 ;
   - le profil système : OS, `layout_id` exigé, réseau, règles de sécurité ;
   - les composants épinglés par version, avec leur configuration déclarative ;
   - le contrat d'entrées fonctionnelles (catégorie A) ;
   - les paramètres résolus par environnement : niveaux de service, nommage, placement, approbation ;
   - les références de secrets, jamais de valeurs ;
   - les contrôles de santé ;
   - la politique de déploiement : cibles admissibles, approbation, vagues (`single` au lot 1), conditions d'arrêt, reprise ;
   - la liste des sorties publiables (`outputs_allowlist`).
2. **Registre de composants versionnés** (`rackloom.component/v1`) : OS et prérequis pris en charge, contrat de paramètres (types, valeurs, défauts, conditions, références de secrets), dépendances et incompatibilités, effets attendus (paquets, fichiers, services, ports, redémarrage), contrôles après installation, implémentation (rôle Ansible épinglé par empreinte) et `verification_level` calculé depuis les preuves enregistrées. Les formulaires et vues de contrat sont générés par un moteur unique depuis ces contrats.
3. **Image vSphere ≠ template Rackloom.** L'image est un point de départ inventorié, avec des attributs déclarés à sa construction (OS, `layout_id`, outils présents). Le partitionnement exact vient d'une image construite avec ce layout ou d'une installation automatisée, jamais d'un rôle Ansible appliqué après coup. Le repartitionnement implicite d'un serveur existant est refusé ; il fait l'objet d'une migration distincte.
4. **Cycle de vie** : `validated` → `published` → `deprecated`, et `withdrawn` pour un retrait. `draft`, `lab_tested` et `in_review` sont réservés à l'atelier. Toute modification produit une nouvelle version. Le passage d'un déploiement à une nouvelle version est un changement distinct, jamais automatique à la publication.
5. **Source et publication au lot 1** : les versions sont écrites et revues dans `catalog/` (Git). Le chargeur valide les contrats, calcule les empreintes, refuse les références hors registre, les secrets littéraux et toute modification d'une version publiée. Publier, déprécier et retirer sont des actions d'administration avec motif, aperçu d'impact et audit, qui ne créent aucun job.
6. **Trois droits distincts** : déployer (`operator`), concevoir (`template_author`, AT01) et publier (`template_publisher`), plus l'approbation (`approver`). Publier ne déploie rien ; approuver un déploiement ne permet pas de modifier un template ; un auteur ne publie pas son propre brouillon.
7. **Exécution** : les variables Terraform viennent de la révision ; les variables Ansible viennent de la seule configuration déclarée des composants. Les empreintes du template et des composants entrent dans le dossier d'exécution.
8. **Santé et état du service** : les contrôles de santé du template sont la seule source de l'état du service d'un déploiement (prêt, créé mais pas prêt, échec, vérification indisponible), distinct du succès des jobs (`docs/architecture.md` §8). La fiche service (F57) le lit sans le recalculer.
9. **Observation d'un serveur de référence (F60)** : les collecteurs utilisés par l'atelier sont des composants d'observation approuvés, versionnés, en lecture seule, avec des champs sur liste blanche (`docs/architecture.md` §12.1). Les valeurs propres à l'hôte ne sont jamais recopiées dans un brouillon.

## Conséquences

- Le lot 1 livre le modèle de données, le chargeur, la publication, un composant `nginx` (1.0.0 et 1.1.0), un composant `fail2ban` optionnel et le template `ubuntu-web` (1.0.0 publié, 1.1.0 candidat). L'atelier n'est pas livré.
- Un logiciel non couvert par le registre est « non pris en charge » : on ajoute un composant revu, jamais un argument libre, un script ou du YAML exécutable.
- Le nombre de versions croît ; la dépréciation et le retrait doivent rester lisibles (historique, impact).
- Les entrées F01 à F30 du backlog parlent de « blueprint » : il s'agit d'une version de template Rackloom. F01 (catalogue Git avec promotion) et F07 (fabrique d'images) s'appuient sur ce modèle.

## Alternatives écartées

- Template vSphere comme unité de catalogue : le résultat (logiciels, contrôles, politique) n'est ni décrit ni vérifiable.
- Un écran par logiciel : coût linéaire, incohérences ; les contrats génèrent les formulaires.
- Versions mutables avec historique : un plan approuvé deviendrait non reproductible.
- Atelier dès le lot 1 : élargit le périmètre avant que le parcours de bout en bout existe.

## À trancher

- Fail2ban au lot 1 (si le reste de E10 est vert) ou directement dans AT01.
- Signature des versions publiées (avec AT01 ou F17) et exigence de double validation pour publier.
- Source des contrats d'interfaces de rôles Ansible existants (`meta/argument_specs.yml`) : réutilisation directe ou conversion.
