#!/bin/sh
# Démarrage du nœud : clé d'hôte propre au nœud (persistée dans /var/lib/lab-node),
# clés autorisées montées en lecture seule depuis /lab-keys, services déjà installés
# relancés (persistance au redémarrage du conteneur), puis sshd au premier plan.
set -eu
install -d -m 0755 /var/lib/lab-node /run/sshd /etc/ssh/authorized
if [ ! -f /var/lib/lab-node/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /var/lib/lab-node/ssh_host_ed25519_key -C "$(hostname)"
fi
cp /var/lib/lab-node/ssh_host_ed25519_key.pub /var/lib/lab-node/host_key.pub
for user in rackloom-collect rackloom-config; do
  if [ -f "/lab-keys/$user.pub" ]; then
    install -m 0644 "/lab-keys/$user.pub" "/etc/ssh/authorized/$user"
  fi
done
if [ -x /etc/init.d/nginx ] && [ -f /var/lib/lab-node/nginx-enabled ]; then
  /etc/init.d/nginx start || true
fi
exec /usr/sbin/sshd -D -e
