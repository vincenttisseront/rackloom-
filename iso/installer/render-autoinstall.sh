#!/usr/bin/env bash
# Rend /autoinstall.yaml complet et non interactif à partir des réponses.
# shellcheck shell=bash

render_network_yaml() {
  local mode=$1 iface=$2 address=$3 gateway=$4 nameservers=$5
  if [ "$mode" = static ]; then
    local ns_yaml="" ns
    for ns in $nameservers; do
      ns_yaml="${ns_yaml}
        - ${ns}"
    done
    cat <<EOF
network:
  version: 2
  ethernets:
    ${iface}:
      dhcp4: false
      addresses:
        - ${address}
      routes:
        - to: default
          via: ${gateway}
      nameservers:
        addresses:${ns_yaml}
EOF
  else
    cat <<'EOF'
network:
  version: 2
  ethernets:
    rl-dhcp-en:
      match:
        name: "en*"
      dhcp4: true
    rl-dhcp-eth:
      match:
        name: "eth*"
      dhcp4: true
EOF
  fi
}

render_autoinstall() {
  local answers=$1 template=$2 out=$3
  # shellcheck disable=SC1090
  source "$answers"

  [ -n "${HOSTNAME:-}" ] || die "HOSTNAME manquant"
  [ -n "${PASSWORD_HASH:-}" ] || die "PASSWORD_HASH manquant"
  [ -n "${DISK_PATH:-}" ] || die "DISK_PATH manquant"
  [ -f "$template" ] || die "modèle autoinstall absent: $template"

  local net_block pass_q host_q real_q user_q disk_q
  net_block=$(render_network_yaml \
    "${NETWORK_MODE:-dhcp}" \
    "${IFACE:-}" \
    "${ADDRESS:-}" \
    "${GATEWAY:-}" \
    "${NAMESERVERS:-}")

  pass_q=$(yaml_single_quote "$PASSWORD_HASH")
  host_q=$(yaml_single_quote "$HOSTNAME")
  real_q=$(yaml_single_quote "${REALNAME:-Rackloom}")
  user_q=$(yaml_single_quote "${USERNAME:-rackloom}")
  disk_q=$(yaml_single_quote "$DISK_PATH")
  # Hostname déjà validé RFC 1123 : sûr à injecter brut dans late-commands.
  host_raw=$HOSTNAME

  local tmp
  tmp=$(mktemp)
  # Remplacements dans le modèle (marqueurs ASCII stables).
  sed \
    -e "s|__RACKLOOM_HOSTNAME_RAW__|${host_raw}|g" \
    -e "s|__RACKLOOM_HOSTNAME__|${host_q}|g" \
    -e "s|__RACKLOOM_USERNAME__|${user_q}|g" \
    -e "s|__RACKLOOM_REALNAME__|${real_q}|g" \
    -e "s|__RACKLOOM_PASSWORD_HASH__|${pass_q}|g" \
    -e "s|__RACKLOOM_DISK_PATH__|${disk_q}|g" \
    "$template" >"$tmp"

  # Injecte le bloc réseau à la place du marqueur (peut être multi-lignes).
  awk -v net="$net_block" '
    /^__RACKLOOM_NETWORK_BLOCK__$/ { print net; next }
    { print }
  ' "$tmp" >"$out"

  rm -f "$tmp"
  chmod 0600 "$out"
}
