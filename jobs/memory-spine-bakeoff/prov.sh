#!/usr/bin/env bash
# Provision isolated bake-off infra in the db-host Postgres 16 instance.
# Reads the ephemeral bakeoff-role password from stdin (never argv/output).
# Idempotent. Creates: role `bakeoff`, DBs mimir_bakeoff + cognee_bakeoff,
# pgvector extension in each. Zero touch on the production `db-host` DB.
set -euo pipefail
read -r NEWPW || true
[ -n "${NEWPW:-}" ] || { echo "ERR: no password on stdin"; exit 2; }

# helper: pipe SQL (stdin) into a given DB inside db-host-db as superuser db-host
psql_db() {
  docker exec -i db-host-db bash -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -U db-host -d "$1" -qtAX -v ON_ERROR_STOP=1 -f -' _ "$1"
}
scalar() {
  docker exec -i db-host-db bash -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -U db-host -d postgres -qtAX -c "$1"' _ "$1"
}

# 1. role (idempotent)
umask 077
cat <<SQLEOF | psql_db postgres
DO \$do\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bakeoff') THEN
    CREATE ROLE bakeoff LOGIN PASSWORD '${NEWPW}';
  ELSE
    ALTER ROLE bakeoff WITH LOGIN PASSWORD '${NEWPW}';
  END IF;
END \$do\$;
SQLEOF
echo "role bakeoff: ready"

# 2. databases (CREATE DATABASE cannot run in a txn / DO block -> shell-guard)
for db in mimir_bakeoff cognee_bakeoff; do
  ex=$(scalar "SELECT 1 FROM pg_database WHERE datname='${db}'" || true)
  if [ "${ex}" != "1" ]; then
    scalar "CREATE DATABASE ${db} OWNER bakeoff" >/dev/null
    echo "db ${db}: created"
  else
    echo "db ${db}: exists"
  fi
  # 3. pgvector + schema privileges inside each DB
  cat <<SQLEOF | psql_db "${db}"
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
GRANT ALL ON SCHEMA public TO bakeoff;
ALTER SCHEMA public OWNER TO bakeoff;
SQLEOF
  echo "db ${db}: vector+pg_trgm ready, public owned by bakeoff"
done
echo "PROVISION_OK"
