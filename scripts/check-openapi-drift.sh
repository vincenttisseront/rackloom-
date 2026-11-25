#!/usr/bin/env bash
# Le contrat committé (openapi/openapi.json) et le client typé (web/generated/schema.d.ts) suivent le code.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

(cd "$ROOT/backend" && uv run rackloom openapi export --output "$TMP/openapi.json" >/dev/null)
if ! diff -u "$ROOT/openapi/openapi.json" "$TMP/openapi.json" > "$TMP/contrat.diff"; then
  head -60 "$TMP/contrat.diff"
  echo "dérive OpenAPI : lancer 'make openapi' et committer le résultat" >&2
  exit 1
fi
(cd "$ROOT/web" && npx --no-install openapi-typescript "$ROOT/openapi/openapi.json" -o "$TMP/schema.d.ts" \
  >/dev/null 2>&1)
if ! diff -q "$ROOT/web/generated/schema.d.ts" "$TMP/schema.d.ts" >/dev/null; then
  echo "client typé périmé : lancer 'make openapi' et committer web/generated/" >&2
  exit 1
fi
echo "OpenAPI : contrat et client typé à jour"
