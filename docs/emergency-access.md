# Accès d'urgence (F14)

Procédure locale à durée limitée lorsque le SSO (IdP) est indisponible. Les comptes
locaux restent le secours ; l'activation se fait **depuis la console** de l'appliance,
jamais depuis une session web anonyme.

## Qui peut activer

Un opérateur ayant un accès physique ou console à l'hôte Rackloom (systemd / `rackloom`
CLI dans le conteneur `api` ou `cli`). L'activation est auditée avec l'acteur `console`.

## Déroulement

1. **Constater** que l'IdP ne répond plus (ou simuler en laboratoire).
2. **Activer** une intervention, avec motif, durée (5 min à 8 h) et allowlist d'e-mails locaux :

```bash
rackloom emergency simulate-idp-down   # laboratoire uniquement
rackloom emergency activate \
  --reason "SSO indisponible : bascule d'urgence" \
  --minutes 60 \
  --user admin@example.invalid
```

3. Les utilisateurs de l'allowlist se connectent avec leur **mot de passe local**.
4. **Fermer** l'intervention dès que le SSO est rétabli :

```bash
rackloom emergency close --reason "SSO rétabli, fin d'intervention"
```

La fermeture invalide les sessions des comptes de l'allowlist (`privilege_epoch`) et
remet `sso.available` à vrai. Tant que le SSO est marqué indisponible et qu'aucune
intervention n'est ouverte, toute connexion locale est refusée (`emergency_closed`).

## Audit

| Action | Entrée d'audit |
|---|---|
| Marquage SSO | `sso.availability` |
| Activation | `emergency.activate` (motif, expiration, utilisateurs) |
| Connexion pendant l'intervention | `auth.login` avec `via=emergency_active` |
| Fermeture | `emergency.close` |

Consulter : Administration › Audit, ou `GET /api/v1/admin/emergency` (admin de plateforme).

## Laboratoire

Sans IdP d'entreprise (lot 4), `simulate-idp-down` pose le drapeau local. Aucune
ressource réelle n'est touchée. Niveau de preuve : **testé localement**.
