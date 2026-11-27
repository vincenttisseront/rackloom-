# Rackloom appliance (Lot 3)

Fichiers déployés par l'ISO autoinstall Ubuntu 24.04 LTS amd64.

- Compose : `/opt/rackloom/compose/compose.yaml`
- Secrets DB : `/etc/rackloom/secrets/` (générés au premier boot, mode 0400)
- Clés applicatives : volumes Docker `rackloom_keys_*` (hors base)
- TLS : `/etc/rackloom/tls/server.{crt,key}`
- Premier boot : `systemctl status rackloom-firstboot`
- Service : `systemctl status rackloom`

Voir la documentation de déploiement fournie avec la livraison.
