#!/usr/bin/env bash
# Démarre la démonstration locale de Rackloom (profil demo, ressources jetables uniquement).
# Idempotent : relancer conserve la base, les clés et les mots de passe déjà générés.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LAB="$ROOT/.lab"
COMPOSE=(docker compose -f "$ROOT/compose/compose.yaml" -f "$ROOT/compose/compose.demo.yaml")
[ -n "${RACKLOOM_COMPOSE_EXTRA:-}" ] && COMPOSE+=(-f "$RACKLOOM_COMPOSE_EXTRA")
NODES=()
for i in $(seq 1 "${RACKLOOM_LAB_NODE_COUNT:-3}"); do NODES+=("lab-node-$i"); done

say() { printf '\n== %s\n' "$*"; }

say "Secrets locaux (.lab/, ignoré par Git)"
install -d -m 0700 "$LAB" "$LAB/secrets"
for name in pg_owner pg_api pg_worker; do
  if [ ! -s "$LAB/secrets/$name" ]; then
    (umask 022; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$LAB/secrets/$name")
  fi
done

if [ "${RACKLOOM_SKIP_BUILD:-0}" != 1 ]; then
  say "Construction des images"
  "$ROOT/scripts/build-images.sh"
fi

say "Base de données, clés, migrations"
"${COMPOSE[@]}" up -d --wait postgres
"${COMPOSE[@]}" run --rm --no-deps init >/dev/null
"${COMPOSE[@]}" run --rm cli migrate

say "Registre et profil de démonstration (publication de ubuntu-web 1.0.0)"
seed=$("${COMPOSE[@]}" run --rm --user "$(id -u):$(id -g)" cli demo seed --root /opt/rackloom \
  --credentials-file /lab/demo-credentials.txt)
echo "$seed"

say "Laboratoire : identités SSH, nœuds, ancres de clés d'hôte"
if ! "${COMPOSE[@]}" run --rm cli lab keys --pub-dir /lab-keys --if-missing; then
  echo "échec de la génération des identités de labo" >&2; exit 1
fi
"${COMPOSE[@]}" --profile lab up -d "${NODES[@]}"
for node in "${NODES[@]}"; do
  key=""
  for _ in $(seq 1 60); do
    key=$("${COMPOSE[@]}" exec -T "$node" cat /var/lib/lab-node/host_key.pub 2>/dev/null || true)
    [ -n "$key" ] && break
    sleep 0.5
  done
  [ -n "$key" ] || { echo "clé d'hôte absente sur $node" >&2; exit 1; }
  printf '%s\n' "$key" | "${COMPOSE[@]}" run --rm -T cli lab anchor --address "$node"
done
"${COMPOSE[@]}" run --rm cli lab nodes "${NODES[@]}"

say "Services de l'appliance"
"${COMPOSE[@]}" up -d --wait caddy api worker launcher
"${COMPOSE[@]}" cp caddy:/data/caddy/pki/authorities/local/root.crt "$LAB/ca.crt" >/dev/null
chmod 0644 "$LAB/ca.crt"

cat <<EOF

Rackloom est prêt : https://localhost:8443  (CA locale : .lab/ca.crt)

Identités de démonstration (mots de passe générés pour cette installation) :
$(sed 's/^/  /' "$LAB/demo-credentials.txt")

Étapes SIMULÉES (badge « Simulé » dans l'interface) :
  - connecteur vSphere fictif (vsphere-fake) : inventaire, test de connexion, suivi ;
  - personnalisation invitée et attribution d'adresse (VM jouées par des conteneurs lab-node) ;
  - assistant : adaptateur fictif déterministe (aucun modèle Ollama démarré).
Étapes réelles sur ressources locales jetables : plan et apply Terraform (provider local),
SSH avec clé d'hôte ancrée et certificats utilisateur courts (F15), Ansible (nginx depuis
un dépôt apt local signé), sonde HTTP.

Arrêt : make down   (conserve les volumes)   |   Remise à zéro : make demo-reset
EOF
