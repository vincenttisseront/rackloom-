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
VENV_PY="$ROOT/.venv/bin/python"

echo "Rackloom doctor — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "== Conteneurs =="
check_cmd docker 1 24.0 docker version --format '{{.Client.Version}}'
check_cmd "docker compose" 1 2.20 docker compose version --short
if docker info >/dev/null 2>&1; then
  ok "docker daemon" "joignable"
else
  fail "docker daemon" "injoignable (démarrer dockerd ou ajouter l'utilisateur au groupe docker)"
fi

echo
echo "== Langages =="
check_cmd python3 1 3.12 python3 --version
check_cmd node 1 22.0 node --version
check_cmd npm 1 10.0 npm --version
check_cmd pnpm 0 9.0 pnpm --version
if [ -x "$VENV_PY" ]; then
  ok "venv" "$ROOT/.venv"
else
  warn "venv" "absent : lancer 'make venv'"
fi

echo
echo "== Base de données (dev natif, optionnel en mode Compose) =="
check_cmd psql 0 16.0 psql --version
check_cmd postgres 0 16.0 bash -c 'ls /usr/lib/postgresql/*/bin/postgres >/dev/null 2>&1 && /usr/lib/postgresql/*/bin/postgres --version'

echo
echo "== Outils d'exécution =="
check_cmd terraform 1 1.6 terraform version
check_cmd ansible-playbook 1 2.16 ansible-playbook --version
if [ -x "$ROOT/.venv/bin/ansible-runner" ]; then
  check_cmd ansible-runner 1 2.3 "$ROOT/.venv/bin/ansible-runner" --version
else
  check_cmd ansible-runner 1 2.3 ansible-runner --version
fi
check_cmd ssh-keygen 1 "" bash -c 'ssh -V 2>&1'
check_cmd sshd 0 "" bash -c '/usr/sbin/sshd -V 2>&1 || true'

echo
echo "== Navigateur (tests e2e) =="
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 \
   || ls "$HOME"/.cache/ms-playwright/chromium-* >/dev/null 2>&1; then
  ok "chromium" "disponible"
else
  warn "chromium" "absent : 'npx playwright install chromium' pour make test-e2e"
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
