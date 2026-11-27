#!/bin/sh
# Démarrage du nœud : clé d'hôte propre au nœud (persistée dans /var/lib/lab-node),
# clés autorisées ou CA utilisateur F15 montées en lecture seule depuis /lab-keys,
# services déjà installés relancés (persistance au redémarrage du conteneur),
# puis sshd au premier plan.
# sshd journalise dans /var/log/auth.log, comme sur un serveur Ubuntu (lu par Fail2ban).
set -eu
install -d -m 0755 /var/lib/lab-node /run/sshd /etc/ssh/authorized /etc/ssh/authorized_principals
if [ ! -f /var/lib/lab-node/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /var/lib/lab-node/ssh_host_ed25519_key -C "$(hostname)"
fi
cp /var/lib/lab-node/ssh_host_ed25519_key.pub /var/lib/lab-node/host_key.pub

if [ -f /lab-keys/ca.pub ]; then
  # F15 : CA approuvée + principals identité@hôte ; pas de clés longue durée.
  install -m 0644 /lab-keys/ca.pub /etc/ssh/rackloom_user_ca.pub
  host="$(hostname)"
  for user in rackloom-collect rackloom-config; do
    printf '%s@%s\n' "$user" "$host" > "/etc/ssh/authorized_principals/$user"
    chmod 0644 "/etc/ssh/authorized_principals/$user"
  done
  cat > /etc/ssh/sshd_config.d/20-rackloom-ca.conf <<'EOF'
TrustedUserCAKeys /etc/ssh/rackloom_user_ca.pub
AuthorizedPrincipalsFile /etc/ssh/authorized_principals/%u
AuthorizedKeysFile none
EOF
else
  rm -f /etc/ssh/sshd_config.d/20-rackloom-ca.conf
  for user in rackloom-collect rackloom-config; do
    if [ -f "/lab-keys/$user.pub" ]; then
      install -m 0644 "/lab-keys/$user.pub" "/etc/ssh/authorized/$user"
    fi
  done
fi

[ -f /var/log/auth.log ] || install -m 0640 -g adm /dev/null /var/log/auth.log
if [ -x /etc/init.d/nginx ] && [ -f /var/lib/lab-node/nginx-enabled ]; then
  /etc/init.d/nginx start || true
fi
if [ -x /etc/init.d/fail2ban ] && [ -f /var/lib/lab-node/fail2ban-enabled ]; then
  /etc/init.d/fail2ban start || true
fi
exec /usr/sbin/sshd -D -E /var/log/auth.log
