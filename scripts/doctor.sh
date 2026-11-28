#!/usr/bin/env bash
# Vérifie l'outillage de développement de Rackloom. Lecture seule : n'installe rien.
# Code de sortie non nul si un prérequis obligatoire manque.
set -u

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; YELLOW=""; RESET=""; }

FAILURES=0
WARNINGS=0

ok()   { printf '%s[ OK ]%s %-22s %s\n' "$GREEN" "$RESET" "$1" "$2"; }
warn() { printf '%s[WARN]%s %-22s %s\n' "$YELLOW" "$RESET" "$1" "$2"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '%s[FAIL]%s %-22s %s\n' "$RED" "$RESET" "$1" "$2"; FAILURES=$((FAILURES + 1)); }

# version_ge 1.2.3 1.2 -> vrai si $1 >= $2
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

check_cmd() {
  # check_cmd <nom> <obligatoire:1|0> <version minimale> <commande de version...>
  local name=$1 required=$2 min=$3; shift 3
  local out version
  if ! out=$("$@" 2>/dev/null | head -n1); then out=""; fi
  version=$(printf '%s' "$out" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1)
  if [ -z "$version" ]; then
    if [ "$required" = 1 ]; then fail "$name" "absent"; else warn "$name" "absent (optionnel)"; fi
    return
  fi
  if [ -n "$min" ] && ! version_ge "$version" "$min"; then
    if [ "$required" = 1 ]; then fail "$name" "$version < $min"; else warn "$name" "$version < $min"; fi
    return
  fi
  ok "$name" "$version"
}

ROOT=$(cd "$(dirname "$0")/.." && pwd)

echo "Rackloom doctor — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "== Conteneurs =="
check_cmd docker 1 24.0 docker version --format '{{.Client.Version}}'
check_cmd "docker compose" 1 2.20 docker compose version --short
if docker info >/dev/null 2>&1; then
  ok "docker daemon" "joignable"
  if [ "$(docker info --format '{{.CgroupVersion}}' 2>/dev/null)" = 2 ]; then
    ok "cgroup" "v2"
  else
    fail "cgroup" "v2 requis (limites des runners)"
  fi
  missing=""
  for image in backend launcher runner-terraform runner-ansible lab-node web; do
    docker image inspect "rackloom/$image:dev" >/dev/null 2>&1 || missing="$missing $image"
  done
  if [ -z "$missing" ]; then ok "images" "rackloom/*:dev construites"; else warn "images" "absentes :$missing ('make images')"; fi
else
  fail "docker daemon" "injoignable (démarrer dockerd ou ajouter l'utilisateur au groupe docker)"
fi

echo
echo "== Langages =="
check_cmd python3 1 3.12 python3 --version
check_cmd uv 1 0.5 uv --version
check_cmd node 1 22.0 node --version
check_cmd npm 1 10.0 npm --version
if [ -x "$ROOT/backend/.venv/bin/python" ]; then
  ok "venv" "backend/.venv"
else
  warn "venv" "absent : lancer 'make venv'"
fi
if [ -d "$ROOT/web/node_modules" ]; then ok "web" "dépendances installées"; else warn "web" "lancer 'make venv'"; fi

echo
echo "== Outils natifs (optionnels : runners Terraform et Ansible conteneurisés) =="
check_cmd terraform 0 1.6 terraform version
check_cmd ansible-playbook 0 2.16 ansible-playbook --version
check_cmd psql 0 16.0 psql --version
check_cmd postgres 0 16.0 bash -c 'ls /usr/lib/postgresql/*/bin/postgres >/dev/null 2>&1 && /usr/lib/postgresql/*/bin/postgres --version'
check_cmd ssh-keygen 1 "" bash -c 'ssh -V 2>&1'

echo
echo "== ISO appliance (lot 3, optionnels hors build) =="
check_cmd xorriso 0 "" xorriso -version
check_cmd qemu-system-x86_64 0 "" qemu-system-x86_64 --version
if [ -f /usr/share/OVMF/OVMF_CODE_4M.fd ]; then
  ok "OVMF" "OVMF_CODE_4M.fd"
else
  warn "OVMF" "absent (/usr/share/OVMF/OVMF_CODE_4M.fd) — requis pour make test-iso"
fi
if [ -e "$ROOT/iso/installer" ]; then
  fail "iso/installer" "installateur maison présent — interdit (Subiquity uniquement)"
else
  ok "iso path" "pas d'installateur maison (Subiquity)"
fi

echo
echo "== Navigateur (tests e2e) =="
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 \
   || ls "$HOME"/.cache/ms-playwright/chromium-* >/dev/null 2>&1; then
  ok "chromium" "disponible"
else
  warn "chromium" "absent : 'npx playwright install chromium' pour make test-e2e"
fi
if command -v certutil >/dev/null 2>&1; then
  ok "certutil" "magasin NSS de test (CA locale vérifiée)"
else
  warn "certutil" "absent (libnss3-tools) : requis par make test-e2e"
fi

echo
echo "== Ressources =="
avail_gb=$(df -Pk "$ROOT" | awk 'NR==2 {printf "%d", $4/1024/1024}')
if [ "${avail_gb:-0}" -ge 10 ]; then ok "disque" "${avail_gb} Go libres"; else warn "disque" "${avail_gb} Go libres (<10 Go)"; fi
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [ "${mem_gb:-0}" -ge 8 ]; then ok "mémoire" "${mem_gb} Go"; else warn "mémoire" "${mem_gb} Go (<8 Go)"; fi
for port in 8443; do
  if (exec 3<>/dev/tcp/127.0.0.1/$port) 2>/dev/null; then
    warn "port $port" "déjà utilisé (la démo Compose l'occupe peut-être)"
  else
    ok "port $port" "libre"
  fi
done

echo
echo "== Hygiène des secrets =="
if git -C "$ROOT" ls-files | grep -E '(^|/)(\.env|.*\.pem|.*\.key|id_rsa|id_ed25519)$' | grep -v '\.example$' >/dev/null; then
  fail "secrets suivis" "fichier sensible suivi par git : $(git -C "$ROOT" ls-files | grep -E '(^|/)(\.env|.*\.pem|.*\.key|id_rsa|id_ed25519)$' | tr '\n' ' ')"
else
  ok "secrets suivis" "aucun fichier .env/.pem/.key suivi"
fi

echo
if [ "$FAILURES" -gt 0 ]; then
  echo "${RED}doctor : $FAILURES échec(s), $WARNINGS avertissement(s)${RESET}"
  exit 1
fi
echo "${GREEN}doctor : OK${RESET} ($WARNINGS avertissement(s))"
