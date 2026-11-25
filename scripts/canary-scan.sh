#!/usr/bin/env bash
# T24 : le canari RLCANARY-<32 hex> de l'exécution courante (.lab/e2e-canary) ne doit apparaître nulle part :
# journaux Compose, tables (templates chargés, résumés de plan, conversations), volumes, artefacts de classe B
# déchiffrés, réponses API et flux SSE, sorties Playwright. Seule exception attendue : le journal nginx du
# nœud de labo où test-e2e.sh l'a injecté. Le canari n'est jamais passé en argument ni affiché.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CANARY_FILE="$ROOT/.lab/e2e-canary"
OUT=${1:-$ROOT/.lab/e2e-results}
COMPOSE=(docker compose -f "$ROOT/compose/compose.yaml" -f "$ROOT/compose/compose.demo.yaml")
[ -n "${RACKLOOM_COMPOSE_EXTRA:-}" ] && COMPOSE+=(-f "$RACKLOOM_COMPOSE_EXTRA")
grep -qE '^RLCANARY-[0-9a-f]{32}$' "$CANARY_FILE" || { echo "canari absent ou invalide : $CANARY_FILE" >&2; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
leaks=0

scan() {  # libellé, fichier
  if grep -qaFf "$CANARY_FILE" "$2"; then
    echo "  FUITE    $1"; leaks=$((leaks + 1))
  else
    echo "  absent   $1 ($(wc -c < "$2") octets examinés)"
  fi
}

echo "T24 : recherche du canari"
"${COMPOSE[@]}" --profile lab logs --no-color > "$TMP/logs" 2>&1
scan "journaux Compose (appliance et nœuds)" "$TMP/logs"

"${COMPOSE[@]}" exec -T postgres pg_dump -U rackloom_owner -d rackloom --data-only > "$TMP/db" 2>/dev/null
scan "base de données (toutes les tables, données seules)" "$TMP/db"

for vol in workspaces tfstate demo_infra artifacts lab_keys; do
  docker run --rm -i --network none --entrypoint sh -v "rackloom_$vol:/v:ro" rackloom/backend:dev \
    -c 'find /v -type f -exec cat {} + 2>/dev/null; true' > "$TMP/vol" 2>/dev/null </dev/null
  scan "volume rackloom_$vol (brut)" "$TMP/vol"
done

if "${COMPOSE[@]}" run --rm -T -v rackloom_artifacts:/var/lib/rackloom/artifacts:ro cli artifacts scan \
    < "$CANARY_FILE"; then
  echo "  absent   artefacts de classe B déchiffrés"
else
  echo "  FUITE    artefacts de classe B déchiffrés"; leaks=$((leaks + 1))
fi

if (cd "$ROOT" && uv run --project backend python scripts/canary-api.py); then
  echo "  absent   réponses API et flux SSE (toutes identités de démonstration)"
else
  echo "  FUITE    réponses API et flux SSE"; leaks=$((leaks + 1))
fi

if [ -d "$OUT" ]; then
  : > "$TMP/pw"
  while IFS= read -r -d '' f; do
    case "$f" in
      *.zip) python3 -c 'import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist(): sys.stdout.buffer.write(z.read(n))' "$f" >> "$TMP/pw" ;;
      *.png|*.webm|*.jpeg) ;;
      *) cat "$f" >> "$TMP/pw" ;;
    esac
  done < <(find "$OUT" -type f -print0)
  scan "sorties de test (traces réseau Playwright, journaux Compose avant T25)" "$TMP/pw"
else
  echo "  ignoré   sorties Playwright : $OUT absent"
fi

node=${RACKLOOM_CANARY_NODE:-lab-node-1}
if "${COMPOSE[@]}" exec -T "$node" cat /var/log/nginx/access.log 2>/dev/null | grep -qaFf "$CANARY_FILE"; then
  echo "  témoin   canari présent dans le journal nginx de $node (injection attendue)"
else
  echo "  ÉCHEC    témoin : canari absent du journal nginx de $node, l'injection n'a pas eu lieu"; leaks=$((leaks + 1))
fi

[ "$leaks" -eq 0 ] && echo "T24 : OK" || { echo "T24 : ÉCHEC ($leaks)"; exit 1; }
