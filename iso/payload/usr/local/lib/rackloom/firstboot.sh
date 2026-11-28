#!/usr/bin/env bash
# Premier boot de l'appliance : réseau, DNS, NTP, FQDN, TLS, secrets DB, clés, jeton.
# Aucun secret n'est lu depuis l'ISO ; tout est généré localement (ADR-0004 §6).
set -euo pipefail

STATE_DIR=/var/lib/rackloom
CONF_DIR=/etc/rackloom
SECRETS_DIR="$CONF_DIR/secrets"
TLS_DIR="$CONF_DIR/tls"
COMPOSE_DIR=/opt/rackloom/compose
MARKER="$STATE_DIR/firstboot.done"
ENV_FILE="$CONF_DIR/appliance.env"

log() { printf 'rackloom-firstboot: %s\n' "$*"; }
die() { printf 'rackloom-firstboot: ERREUR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "exécuter en root"
install -d -m 0750 "$STATE_DIR" "$CONF_DIR" "$SECRETS_DIR" "$TLS_DIR" "$CONF_DIR/keys"

if [ -f "$MARKER" ]; then
  log "déjà effectué ($(cat "$MARKER")) — rien à faire"
  exit 0
fi

# --- Réseau / DNS / NTP -------------------------------------------------------
log "vérification réseau"
if ! ip -4 route show default | grep -q .; then
  die "aucune route par défaut IPv4 ; configurez le réseau puis relancez (systemctl start rackloom-firstboot)"
fi

dns_ok=0
for resolver in $(awk '/^nameserver/ {print $2}' /etc/resolv.conf); do
  if getent hosts ubuntu.com >/dev/null 2>&1 || getent hosts example.com >/dev/null 2>&1; then
    dns_ok=1
    break
  fi
done
# Hors ligne toléré si le FQDN est fourni et résolu localement.
if [ "$dns_ok" -eq 0 ]; then
  log "avertissement: résolution DNS externe indisponible (mode déconnecté possible)"
fi

if command -v timedatectl >/dev/null 2>&1; then
  if ! timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qi yes; then
    timedatectl set-ntp true 2>/dev/null || true
    log "NTP activé (synchronisation peut prendre quelques secondes)"
  fi
fi

# --- FQDN / hostname ----------------------------------------------------------
# Priorité : RACKLOOM_FQDN → hostname posé par Subiquity (identity) → invite TTY.
# Idempotent : ne régénère rien si firstboot.done existe (ci-dessus).
FQDN="${RACKLOOM_FQDN:-}"
if [ -z "$FQDN" ]; then
  FQDN=$(hostname -f 2>/dev/null || hostname || true)
fi
if [ -z "$FQDN" ] || [ "$FQDN" = localhost ] || [ "$FQDN" = "(none)" ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    read -r -p "FQDN de l'appliance (ex. rackloom.example.invalid) : " FQDN
  fi
fi
[ -n "$FQDN" ] || die "FQDN requis"
hostnamectl set-hostname "$FQDN" 2>/dev/null || hostname "$FQDN" || true

# --- Secrets PostgreSQL (références fichiers, jamais affichés) ----------------
gen_secret() {
  local dest=$1
  if [ -s "$dest" ]; then return 0; fi
  umask 077
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' >"$dest"
  chmod 0400 "$dest"
}
gen_secret "$SECRETS_DIR/pg_owner"
gen_secret "$SECRETS_DIR/pg_api"
gen_secret "$SECRETS_DIR/pg_worker"
chown -R root:root "$SECRETS_DIR"
chmod 0700 "$SECRETS_DIR"

# --- TLS : certificat admin fourni ou CA locale -------------------------------
if [ ! -s "$TLS_DIR/server.crt" ] || [ ! -s "$TLS_DIR/server.key" ]; then
  if [ -s "$TLS_DIR/admin.crt" ] && [ -s "$TLS_DIR/admin.key" ]; then
    cp -a "$TLS_DIR/admin.crt" "$TLS_DIR/server.crt"
    cp -a "$TLS_DIR/admin.key" "$TLS_DIR/server.key"
    log "TLS : certificat administrateur repris"
  else
    openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
      -keyout "$TLS_DIR/server.key" \
      -out "$TLS_DIR/server.crt" \
      -subj "/CN=$FQDN/O=Rackloom Appliance/C=FR" \
      -addext "subjectAltName=DNS:$FQDN,DNS:localhost,IP:127.0.0.1" \
      >/dev/null 2>&1
    log "TLS : certificat local généré pour $FQDN (remplacez-le via /etc/rackloom/tls si besoin)"
  fi
fi
chmod 0640 "$TLS_DIR/server.key"
chmod 0644 "$TLS_DIR/server.crt"

# --- Caddyfile depuis le modèle ----------------------------------------------
TEMPLATE="$COMPOSE_DIR/caddy/Caddyfile.template"
if [ -f "$TEMPLATE" ]; then
  sed "s/__RACKLOOM_FQDN__/$FQDN/g" "$TEMPLATE" >"$CONF_DIR/Caddyfile"
  chmod 0644 "$CONF_DIR/Caddyfile"
fi

# --- Images Docker embarquées -------------------------------------------------
if [ -x /usr/local/lib/rackloom/load-images.sh ]; then
  /usr/local/lib/rackloom/load-images.sh || die "chargement des images Docker échoué"
fi

# --- Environnement Compose ----------------------------------------------------
umask 077
cat >"$ENV_FILE" <<EOF
RACKLOOM_PUBLIC_ORIGIN=https://$FQDN
RACKLOOM_CHAT_ADAPTER=${RACKLOOM_CHAT_ADAPTER:-disabled}
RACKLOOM_WORKER_SLOTS=${RACKLOOM_WORKER_SLOTS:-2}
EOF
chmod 0640 "$ENV_FILE"

# --- Démarrage pile : postgres → init clés → migrate → services --------------
export $(grep -v '^#' "$ENV_FILE" | xargs -d '\n')
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_DIR/compose.yaml")

log "démarrage PostgreSQL"
"${COMPOSE[@]}" up -d --wait postgres
log "initialisation des clés (hors base)"
"${COMPOSE[@]}" run --rm --no-deps init
log "migrations"
"${COMPOSE[@]}" run --rm --no-deps cli migrate
log "services api / worker / launcher / caddy"
"${COMPOSE[@]}" up -d --wait caddy api worker launcher

# --- Jeton d'amorçage (affiché une fois sur la console) -----------------------
TOKEN=""
if TOKEN=$("${COMPOSE[@]}" run --rm --no-deps cli bootstrap-token 2>/dev/null); then
  :
else
  TOKEN="(administrateur déjà présent — jeton non régénéré)"
fi

date -u +%Y-%m-%dT%H:%M:%SZ >"$MARKER"
chmod 0644 "$MARKER"

cat <<EOF

========================================================================
  Rackloom appliance — premier boot terminé
  URL     : https://$FQDN
  Compte  : créez l'administrateur de plateforme avec le jeton ci-dessous
  Jeton   : $TOKEN
  (usage unique, 30 minutes ; jamais stocké dans un fichier)
========================================================================

EOF
