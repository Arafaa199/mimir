"""Write the mandatory §4 provenance tuple to `mimir.provenance` (spec 05, day-one).

Every unit that enters the spine — cognified OR registered pointer-only — gets exactly
one ledger row, keyed by its content hash. This is the record that makes the estate
walls PROVABLE: a Work offboarding purge is a query over this table plus dropping the
work datasets, and recall-time scope checks read it. It is written in the same step as
the append-only checkpoint, so the two never disagree.

Connection reuses the cognee DB env (`DB_HOST`/`DB_NAME`/… set by with_env_prod.sh) —
the ledger lives in the `mimir` schema of `cognee_prod`, which the `cognee` role owns.
"""
import os

import psycopg2


def _conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


def record(units: list, stage: str = "backfill") -> int:
    """Upsert one row per unit. `units` are dicts with keys: sha256, title, and a `_prov`
    tuple (source, source_trust, estate, sensitivity, dataset, pointer_only).

    ON CONFLICT updates: a shadow pass may re-present a unit already backfilled, and its
    classification is the current truth. Returns the number of rows written.
    """
    if not units:
        return 0
    rows = [
        (u["sha256"], p["source"], p["source_trust"], p["estate"], p["sensitivity"],
         p["dataset"], (u.get("title") or "")[:400], p["pointer_only"], stage)
        for u in units for p in (u["_prov"],)
    ]
    sql = (
        "INSERT INTO mimir.provenance "
        "(sha256, source, source_trust, estate, sensitivity, dataset, title, "
        " pointer_only, stage) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (sha256) DO UPDATE SET "
        "  source=EXCLUDED.source, source_trust=EXCLUDED.source_trust, "
        "  estate=EXCLUDED.estate, sensitivity=EXCLUDED.sensitivity, "
        "  dataset=EXCLUDED.dataset, title=EXCLUDED.title, "
        "  pointer_only=EXCLUDED.pointer_only, stage=EXCLUDED.stage, "
        "  cognified_at=now()"
    )
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.executemany(sql, rows)
        return len(rows)
    finally:
        conn.close()


def counts() -> dict:
    """Current ledger population by dataset — for status/verification."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT dataset, count(*) FROM mimir.provenance GROUP BY dataset")
            return dict(cur.fetchall())
    finally:
        conn.close()
