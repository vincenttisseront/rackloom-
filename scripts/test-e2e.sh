#!/usr/bin/env bash
# make test-e2e : pile de test jetable (6 nœuds de labo), vrais runners Terraform et Ansible, parcours
# Playwright §1.1 et D1 à D6 avec contrôle axe, redémarrage pendant une exécution (T25), scan du canari (T24)
# et exposition (T28). DÉTRUIT les volumes rackloom_* : ressources locales jetables uniquement.
# Arguments supplémentaires transmis à Playwright (ex. : scripts/test-e2e.sh d2-verrouillage).
# RACKLOOM_E2E_REUSE=1 réutilise la pile en place ; RACKLOOM_E2E_VIDEO=1 enregistre les vidéos.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LAB="$ROOT/.lab"
export RACKLOOM_COMPOSE_EXTRA="$ROOT/compose/compose.test.yaml"
export RACKLOOM_LAB_NODE_COUNT=6
OUT=${RACKLOOM_E2E_OUTPUT:-$LAB/e2e-results}
COMPOSE=(docker compose -f "$ROOT/compose/compose.yaml" -f "$ROOT/compose/compose.demo.yaml"
         -f "$RACKLOOM_COMPOSE_EXTRA")
results=()
failed=0

say() { printf '\n== %s\n' "$*"; }
step() {  # libellé, commande…
  local label=$1; shift
  say "$label"
  if "$@"; then results+=("OK      $label"); else results+=("ÉCHEC   $label"); failed=1; fi
}

if [ "${RACKLOOM_E2E_REUSE:-0}" != 1 ]; then
  say "Pile de test jetable (suppression des volumes rackloom_*)"
  "${COMPOSE[@]}" --profile lab --profile tools down -v --remove-orphans
  install -d -m 0700 "$LAB"
  # Le journal contient les mots de passe générés : il reste dans .lab/ (ignoré par Git).
  if ! "$ROOT/scripts/demo.sh" > "$LAB/e2e-demo.log" 2>&1; then
    grep -v '@demo.invalid' "$LAB/e2e-demo.log" | tail -40; echo "démarrage de la pile de test en échec" >&2; exit 1
  fi
fi

say "Canari T24 (valeur aléatoire de cette exécution, .lab/e2e-canary)"
(umask 077; printf 'RLCANARY-%s\n' "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" > "$LAB/e2e-canary")
(cd "$ROOT" && uv run --project backend python scripts/canary-api.py --plant) || exit 1
sed 's|.*|10.0.0.1 - - [25/Nov/2026:10:00:00 +0000] "GET /?jeton=& HTTP/1.1" 404 0 "-" "canari"|' \
  "$LAB/e2e-canary" | "${COMPOSE[@]}" exec -T lab-node-1 sh -c 'mkdir -p /var/log/nginx && cat >> /var/log/nginx/access.log'

say "Navigateur : magasin NSS de test qui fait confiance à .lab/ca.crt (vérification TLS active)"
NSS="$LAB/e2e-home/.pki/nssdb"
rm -rf "$NSS" && mkdir -p "$NSS"
certutil -N -d "sql:$NSS" --empty-password
certutil -A -d "sql:$NSS" -n "Rackloom demo CA" -t "C,," -i "$LAB/ca.crt"

rm -rf "$OUT" && mkdir -p "$OUT"
# T25 d'abord (D5 retire ubuntu-web 1.0.0). Il supprime les conteneurs : leurs journaux sont conservés
# pour le scan du canari (T24).
"${COMPOSE[@]}" --profile lab logs --no-color > "$OUT/journaux-compose-avant-T25.log" 2>&1
step "T25 : redémarrage de l'appliance pendant la configuration Ansible" \
  env -C "$ROOT" uv run --project backend python scripts/smoke-d1.py --restart-during ansible_configure
# Playwright vide son dossier de sortie au démarrage : sous-dossier distinct du journal ci-dessus.
step "T30-T36 : Playwright §1.1 et D1 à D6 (axe)" \
  env -C "$ROOT/web" RACKLOOM_E2E_HOME="$LAB/e2e-home" NODE_EXTRA_CA_CERTS="$LAB/ca.crt" \
  RACKLOOM_E2E_OUTPUT="$OUT/playwright" RACKLOOM_E2E_TRACE=1 npx playwright test --reporter=list "$@"
step "T24 : scan du canari" "$ROOT/scripts/canary-scan.sh" "$OUT"
step "T28 : exposition" "$ROOT/scripts/check-exposure.sh"

say "Bilan"
printf '  %s\n' "${results[@]}"
exit "$failed"
