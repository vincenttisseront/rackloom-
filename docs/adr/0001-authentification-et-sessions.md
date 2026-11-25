# ADR-0001 : authentification et sessions

- Statut : proposé (lot 0, 2026-11-25)
- Décideur : Vincent

## Contexte

L'appliance doit fonctionner dès le premier boot sans IdP, puis s'intégrer à un SSO d'entreprise. Les rôles (administrateur de plateforme, opérateur, approbateur, publicateur et auteur de templates, auditeur, identités de service) sont contrôlés côté serveur par projet et environnement. Déployer, concevoir et publier sont trois droits distincts (ADR-0009). L'approbation doit être une action authentifiée, distincte du demandeur en production.

## Décision

1. **Amorçage** : `rackloom bootstrap-token` affiche sur la console un jeton aléatoire (256 bits), stocké haché, valable 30 minutes, à usage unique. Il sert uniquement à créer le premier administrateur. Aucun compte ni mot de passe par défaut.
2. **Comptes locaux** (v1 et secours) : mots de passe argon2id (`argon2-cffi`, paramètres OWASP), limitation de débit et verrouillage temporaire.
3. **Sessions serveur** : identifiant aléatoire stocké haché en base, cookie `__Host-rl_session` HttpOnly, Secure, SameSite=Strict, Path=/ ; expiration d'inactivité 30 min et absolue 12 h ; rotation à la connexion, à l'élévation et à tout changement de rôle ; révocation par l'utilisateur, l'admin ou l'incrément de `privilege_epoch`.
4. **CSRF** : jeton lié à la session, exigé dans l'en-tête `X-CSRF-Token` pour toute méthode non sûre, plus contrôle de l'en-tête `Origin`.
5. **Autorisation** : `role_bindings` (utilisateur, rôle, projet, environnement optionnel). Fonction centrale `authz.require` et dépôts scopés ; hors périmètre 404, droit insuffisant 403. Les droits sont réévalués à l'admission de chaque job. Rôles du lot 1 : `platform_admin`, `operator` (déployer), `approver`, `template_publisher` (publier, déprécier, retirer une version de template), `auditor`. `template_author` (concevoir) est déclaré mais sans route avant l'atelier AT01. Aucun rôle n'autorise à fixer une valeur technique dans une demande. `platform_admin` et `template_publisher` ne lisent pas les données d'un projet sans rôle sur ce projet, et publier n'implique ni déployer ni approuver. Les actions proposées par l'UI sont calculées côté serveur (`workflow.available_actions`) et revérifiées par chaque route à l'appel ; partager une demande ne crée aucun droit (F54).
6. **Séparation des rôles** : `environments.requires_distinct_approver`, imposé `true` pour le type `prod` ; contrôlé à l'approbation et à l'admission.
7. **Contexte d'authentification** enregistré avec chaque approbation (`auth_method`, `auth_time`) pour permettre la réauthentification forte (F12).
8. **Démo** : identités fictives `*.demo.invalid`, mots de passe générés à l'installation ; refusées au démarrage en profil `production`.
9. **Lot 4 : OIDC** avec Authlib (code + PKCE), groupes de l'IdP mappés vers des rôles par projet, `acr` et `auth_time` vérifiés côté serveur. Les comptes locaux restent pour l'accès d'urgence (F14), désactivables. Les accès aux services livrés (F59) passent par une correspondance approuvée et versionnée entre niveau d'accès et groupe d'identité ; « accès accordé » n'est affiché qu'après confirmation de l'IdP, et une relance est idempotente.

## Conséquences

- Pas de jetons porteurs dans le navigateur ; le SPA repose sur le cookie de même origine.
- Les identités de service (worker, launcher) n'ont pas de session web ; elles s'authentifient par rôle base et socket local.
- Sans OIDC, pas de second facteur en v1 : risque accepté au lot 1, à lever avant un usage hors laboratoire.

## Alternatives écartées

- JWT sans état dans le navigateur : révocation et rotation plus difficiles.
- Bibliothèque d'authentification « tout-en-un » maison : préférer des briques maintenues (argon2-cffi, Authlib).

## À trancher

- IdP cible du lot 4 (Entra ID, Keycloak, autre) et format des groupes.
- Exigence d'un second facteur local avant le lot 4.
- Qui tient le rôle `template_publisher` en exploitation (équipe plateforme, référents techniques), et si une publication exige deux personnes.
