#!/usr/bin/env python
"""Drain — cognify what remember() stored (spec 07).

remember() is fast on purpose: it makes a memory DURABLE and immediately RECALLABLE (via the
pgvector floor) in ~200ms, and leaves the expensive part — an LLM reading the text and
extracting a graph, ~140s/doc — to this job.

Run it when the graph is free. It is safe to run any time: kuzu holds an exclusive lock on
whatever estate is being cognified, so a locked estate simply stays pending and is retried.
Nothing is ever lost; nothing is ever cognified twice (the ledger + incremental_loading both
key on content hash).

    ./with_env_prod.sh ./venv/bin/python drain.py            # drain everything free
    ./with_env_prod.sh ./venv/bin/python drain.py --dataset personal
"""
import argparse
import asyncio
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    import cognee
    import provenance as prov

    sql = ("SELECT sha256, title, body, dataset, source, source_trust, estate, sensitivity "
           "FROM mimir.pending_writes WHERE cognified_at IS NULL")
    params = []
    if args.dataset:
        sql += " AND dataset = %s"
        params.append(args.dataset)
    sql += " ORDER BY created_at LIMIT %s"
    params.append(args.limit)

    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("nothing pending")
        return 0
    print(f"{len(rows)} pending write(s)")

    done = failed = 0
    for sha, title, body, dataset, source, trust, estate, sens in rows:
        doc = prov.as_document(source, body, {
            "source": source, "source_trust": trust, "estate": estate,
            "sensitivity": sens, "dataset": dataset, "pointer_only": False,
        })
        try:
            await cognee.add(doc, dataset_name=dataset)
            await cognee.cognify(datasets=[dataset], incremental_loading=True,
                                 data_per_batch=1)
        except Exception as exc:  # noqa: BLE001
            # A locked estate (backfill in progress) is EXPECTED, not a failure. It stays
            # pending and the next drain picks it up.
            failed += 1
            conn = _conn()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("UPDATE mimir.pending_writes SET attempts = attempts + 1, "
                                "last_error = %s WHERE sha256 = %s",
                                (f"{type(exc).__name__}: {str(exc)[:200]}", sha))
            finally:
                conn.close()
            print(f"  deferred [{dataset}] {title[:50]} -> {type(exc).__name__}")
            continue

        conn = _conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE mimir.pending_writes SET cognified_at = now() "
                            "WHERE sha256 = %s", (sha,))
                cur.execute("UPDATE mimir.provenance SET extraction_model = %s "
                            "WHERE sha256 = %s", (os.environ.get("LLM_MODEL"), sha))
        finally:
            conn.close()
        done += 1
        print(f"  cognified [{dataset}] {title[:50]}")

    print(f"\ncognified={done} deferred={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
