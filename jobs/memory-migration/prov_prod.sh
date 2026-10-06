#!/usr/bin/env bash
# Stage A — provision the cognee PROD spine in the db-host Postgres 16 instance.
#
# Strictly additive: creates role `cognee` + DB `cognee_prod` + pgvector/pg_trgm.
# It NEVER touches the production `db-host` database, and never alters an existing
# grant (revoking CONNECT from PUBLIC would hit every other prod consumer).
#
# Blast radius is asserted, not assumed: the `cognee` role is created with no
# privileges on the prod schemas, and the script fails if it can read or write
# memory.entries / search.embeddings.
#
# Reads the role password from stdin (never argv, never stdout, never a file).
# Idempotent.
#
#   openssl rand -base64 24 | ./prov_prod.sh
set -euo pipefail
read -r NEWPW || true
[ -n "${NEWPW:-}" ] || { echo "ERR: no password on stdin"; exit 2; }

DB=cognee_prod
ROLE=cognee

# pipe SQL (stdin) into a given DB inside db-host-db as superuser `db-host`
psql_db() {
  ssh -o ConnectTimeout=8 db-host "docker exec -i db-host-db bash -c \
    'PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U db-host -d \"\$1\" -qtAX -v ON_ERROR_STOP=1 -f -' _ '$1'"
}

umask 077

# 1. role (idempotent)
cat <<SQLEOF | psql_db postgres
DO \$do\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${NEWPW}';
  ELSE
    ALTER ROLE ${ROLE} WITH LOGIN PASSWORD '${NEWPW}';
  END IF;
END \$do\$;
SQLEOF
echo "role ${ROLE}: ready"

# 2. database. CREATE DATABASE cannot run in a txn/DO block; \gexec keeps it idempotent.
cat <<SQLEOF | psql_db postgres
SELECT 'CREATE DATABASE ${DB} OWNER ${ROLE}'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='${DB}')\gexec
SQLEOF
echo "db ${DB}: ready"

# 3. extensions + ownership inside the new DB
cat <<SQLEOF | psql_db "${DB}"
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER SCHEMA public OWNER TO ${ROLE};
GRANT ALL ON SCHEMA public TO ${ROLE};
SQLEOF
echo "db ${DB}: vector+pg_trgm ready, public owned by ${ROLE}"

# 4. Estate isolation needs ENABLE_BACKEND_ACCESS_CONTROL=True, under which cognee
#    creates ONE DATABASE PER DATASET (personal/work/shared) and runs each search
#    in that database's context. So the role needs CREATEDB, and every new dataset
#    DB needs the `vector` extension.
#
#    pgvector is NOT a trusted extension in this image (no `trusted = true` in
#    vector.control), so a non-superuser owner cannot `CREATE EXTENSION vector`.
#    Installing it into `template1` — as superuser, once — makes every future
#    dataset DB inherit it, and cognee's `CREATE EXTENSION IF NOT EXISTS` no-ops.
#    Additive and instance-wide; it does not alter any existing database.
cat <<SQLEOF | psql_db postgres
ALTER ROLE ${ROLE} CREATEDB;
SQLEOF
echo "role ${ROLE}: CREATEDB granted (needed for per-dataset estate DBs)"

cat <<SQLEOF | psql_db template1
CREATE EXTENSION IF NOT EXISTS vector;
SQLEOF
echo "template1: vector installed (new dataset DBs inherit it)"

# backfill any dataset DBs cognee already created before template1 had vector
for d in $(cat <<'SQLEOF' | psql_db postgres
SELECT datname FROM pg_database
 WHERE pg_get_userbyid(datdba) = 'cognee' AND datname <> 'cognee_prod';
SQLEOF
); do
  printf 'CREATE EXTENSION IF NOT EXISTS vector;\n' | psql_db "$d" >/dev/null
  echo "dataset db ${d}: vector ensured"
done

# 4b. Provenance ledger (spec 05 §4). The mandatory (source, source_trust, estate,
#     sensitivity) tuple for EVERY spine unit, written atomically with its cognify.
#     This is the day-one invariant made queryable and PURGEABLE: a Work offboarding
#     is `DELETE FROM mimir.provenance WHERE estate IN ('work','work_confidential')`
#     plus dropping those datasets — provable, because the walls are physical and the
#     ledger records which wall each unit went to. Lives in its own schema so cognee's
#     own migrations (which own `public`) never collide with it.
# psql_db connects as the `db-host` superuser, so this table is owned by db-host. The
# backfill connects as the `cognee` role, so it needs an explicit GRANT (ownership is
# not required for INSERT/SELECT/UPDATE/DELETE, and a purge only needs DELETE). Kept
# db-host-owned deliberately — simpler and idempotent than an ownership handoff.
cat <<SQLEOF | psql_db "${DB}"
CREATE SCHEMA IF NOT EXISTS mimir AUTHORIZATION ${ROLE};
CREATE TABLE IF NOT EXISTS mimir.provenance (
  sha256        text PRIMARY KEY,
  source        text NOT NULL,
  source_trust  text NOT NULL CHECK (source_trust IN ('owner','agent','external','untrusted')),
  estate        text NOT NULL CHECK (estate IN ('personal','work','shared')),
  sensitivity   text NOT NULL CHECK (sensitivity IN ('normal','confidential')),
  dataset       text NOT NULL CHECK (dataset IN ('personal','work','shared','work_confidential')),
  title         text,
  pointer_only  boolean NOT NULL DEFAULT false,
  stage         text NOT NULL DEFAULT 'backfill',
  cognified_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mimir_prov_dataset ON mimir.provenance (dataset);
CREATE INDEX IF NOT EXISTS idx_mimir_prov_estate  ON mimir.provenance (estate);
GRANT USAGE ON SCHEMA mimir TO ${ROLE};
GRANT ALL PRIVILEGES ON mimir.provenance TO ${ROLE};
SQLEOF
echo "mimir.provenance ledger: ready (cognee granted full DML)"

# 5. blast-radius assertion. CONNECT is granted to PUBLIC on every database by
#    default and revoking it would break prod consumers, so assert the property
#    that actually matters: no rights on the production memory tables.
echo "--- blast radius (all must be false) ---"
out=$(cat <<SQLEOF | psql_db db-host
SELECT 'memory.entries    SELECT: '||has_table_privilege('${ROLE}','memory.entries','SELECT')::text;
SELECT 'memory.entries    INSERT: '||has_table_privilege('${ROLE}','memory.entries','INSERT')::text;
SELECT 'memory.entries    UPDATE: '||has_table_privilege('${ROLE}','memory.entries','UPDATE')::text;
SELECT 'memory.entries    DELETE: '||has_table_privilege('${ROLE}','memory.entries','DELETE')::text;
SELECT 'search.embeddings INSERT: '||has_table_privilege('${ROLE}','search.embeddings','INSERT')::text;
SELECT 'search.embeddings DELETE: '||has_table_privilege('${ROLE}','search.embeddings','DELETE')::text;
SELECT 'schema memory     USAGE : '||has_schema_privilege('${ROLE}','memory','USAGE')::text;
SQLEOF
)
echo "$out"
# psql -A prints the boolean as `true`/`false`, NOT `t`/`f`. Match accordingly,
# and fail closed if the probe returned nothing at all.
[ "$(echo "$out" | grep -c ': false$')" = "7" ] || {
  echo "ERR: role ${ROLE} has privileges on production memory (or the probe failed) — refusing"
  exit 3
}

echo "PROVISION_OK"
