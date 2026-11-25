# ADR-0002 : state Terraform et plans

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

Terraform crée et gère les VM. Son state contient des attributs sensibles et doit être chiffré, verrouillé et séparé par stack. La base de Rackloom ne doit pas le remplacer. L'approbation porte sur un plan précis qui doit être appliqué tel quel.

## Décision

1. **Une stack par déploiement** (`deployments.stack_id`), identifiée dans `stacks` avec `backend_kind`, `backend_ref`, et seulement `lineage` et `serial` du dernier state connu. Un second déploiement du même service dans le même environnement crée une nouvelle stack : il ne peut pas planifier la suppression du premier. Modifier un déploiement réutilise sa stack.
2. **Variables générées** : `params.tfvars.json` est produit par le serveur à partir des seules valeurs résolues de la révision (ADR-0008) et du contrat de la version de template (ADR-0009). Aucune valeur ne vient directement d'un demandeur ou du modèle.
3. **Démo et lot 1** : backend `local`, un dossier jetable par stack (`/state` monté dans le seul runner de cette stack), volume `tfstate-demo` distinct de la base. Le module de démonstration `lab-vm` crée des ressources locales jetables, pas des VM. Étiqueté « State de démo » dans l'UI, refusé en profil `production`.
4. **Environnements réels (lot 2)** : backend distant avec chiffrement au repos, TLS, verrouillage natif et préfixe ou espace dédié par stack, avec des identifiants limités à ce préfixe. Recommandation : stockage objet compatible S3 interne avec verrouillage par fichier de verrou (Terraform 1.10 et plus), sinon backend HTTP d'une forge interne. HCP Terraform ou Terraform Enterprise : interface prévue, non implémentée.
5. **Plans** : `plan -out=plan.bin` ; le worker calcule le SHA-256, chiffre `plan.bin` (classe A, worker seul), produit un résumé filtré depuis `show -json`, puis supprime le JSON brut. Ni state ni plan brut dans la base, le chat, les événements ou les journaux.
6. **Application** : uniquement `apply plan.bin` après contrôle du SHA-256 approuvé. Aucun `plan` relancé avant `apply`. Si Terraform refuse le plan périmé, la demande passe en « Plan expiré » et exige un nouveau plan et une nouvelle approbation.
7. **Verrous** : verrou Rackloom par stack (un job d'écriture admis à la fois) en plus du verrou du backend. Un verrou de backend orphelin n'est levé (`force-unlock`) que par un admin, après réconciliation, et l'action est auditée.
8. **Réconciliation** : `tf_state_read` lit le state dans le runner et ne renvoie qu'adresses, `lineage` et `serial`.
9. **Providers et modules** : depuis le catalogue, versions fixes, lockfile avec hachages, miroir local dans l'image runner, aucune installation directe.
10. **Licences** : Terraform (BSL 1.1 depuis la 1.6) est utilisé en développement. Sa redistribution dans l'ISO et celle des providers sont à valider avant le lot 3. OpenTofu (MPL 2.0) est une option **à tester** (compatibilité des providers, du format de plan et du state, chiffrement natif du state), pas un substitut transparent.

## Conséquences

- La restauration de l'appliance et celle du state sont deux procédures distinctes (lot 3).
- Une stack jetable de démo ne garantit pas la durabilité ; elle sert les tests locaux.
- Un changement de version de la CLI ou d'une image runner invalide les plans et approbations en attente (empreinte).
- Le nombre de stacks croît avec le nombre de déploiements ; l'inventaire des stacks et leur retrait (F06) devront le gérer.
- Un changement de niveau de service qui implique un remplacement est bloqué comme toute suppression ou remplacement.

## Alternatives écartées

- State dans la base applicative (backend `pg` sur la même instance) : mélange des responsabilités, sauvegarde et accès communs.
- Recalcul du plan juste avant l'application : contredit l'approbation.
- Une stack par (projet, environnement, service) : un second déploiement modifierait la même stack et pourrait planifier la suppression ou le remplacement des machines du premier.

## À trancher

- Backend distant réel du lot 2 (stockage objet existant, forge, autre).
- Position juridique sur la redistribution de Terraform et des providers dans l'ISO.
