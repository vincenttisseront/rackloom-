#!/bin/sh
# Rôles applicatifs à moindre privilège ; les droits sont accordés par les migrations.
# Mots de passe lus depuis les secrets Compose, générés localement dans .lab/secrets/.
set -eu
api=$(cat /run/secrets/pg_api)
worker=$(cat /run/secrets/pg_worker)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api="$api" -v worker="$worker" <<'EOSQL'
CREATE ROLE rackloom_api LOGIN PASSWORD :'api';
CREATE ROLE rackloom_worker LOGIN PASSWORD :'worker';
REVOKE ALL ON DATABASE rackloom FROM PUBLIC;
GRANT CONNECT ON DATABASE rackloom TO rackloom_api, rackloom_worker;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EOSQL
