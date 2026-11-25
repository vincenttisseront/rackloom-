#!/usr/bin/env bash
# Construit les images locales de Rackloom (étiquette :dev). Aucune image n'est poussée.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

build() {
  local tag=$1 dockerfile=$2 context=${3:-.}
  echo "-- $tag"
  docker build --quiet -f "$dockerfile" -t "$tag" "$context" >/dev/null
}

build rackloom/backend:dev backend/Dockerfile
build rackloom/launcher:dev launcher/Dockerfile
build rackloom/runner-terraform:dev runners/terraform/Dockerfile
build rackloom/runner-ansible:dev runners/ansible/Dockerfile
build rackloom/lab-node:dev lab/images/lab-node/Dockerfile lab/images/lab-node
build rackloom/web:dev compose/caddy/Dockerfile
echo "images construites"
