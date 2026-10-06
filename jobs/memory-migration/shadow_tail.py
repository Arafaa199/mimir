#!/usr/bin/env python
"""Stage C — shadow-write. New writes land in the old stores AND in the cognee spine.

Spec 05 §1(c): during the soak, every new memory must reach cognee while the old
stores stay authoritative. This is a WATERMARK TAILER, not a hook in the write path.

Why a tailer rather than dual-write hooks:
  * There are at least nine writers across three stores (intake /v1/memory/save and
    /batch-save, seed-memory, memory-synthesis, zeroclaw-memory-sync push AND pull,
    ZeroClaw's own memory_store tool, sync-work, zeroclaw-event-sync, plus direct
    SQL). Hooking each one is nine chances to miss a writer, and a missed writer is a
    silent hole in the spine that only surfaces at cutover.
  * A tailer reads what actually landed. It cannot miss a writer it does not know
    about, and it touches no production write path — so it cannot break one.
  * cognee's cost is per-chunk LLM extraction. Design law 2 says reasoning is ONE
    batched pass, not per-event. Re-cognifying on every write would also re-pay for
    the same content each time a doc_seeder run touched it.

Idempotent by construction: cognee's `incremental_loading` keys on a content hash, so
re-presenting text it already processed is a cheap lookup, not another LLM call. The
watermark is therefore an optimisation, not a correctness requirement — overlap is
free, and it is advanced only AFTER the batch is cognified.

  ./run_on_db_host.sh shadow            # one pass
  systemctl --user start mimir-shadow-tail.service
"""
import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cost_meter
import ledger
import nomic_engine
import provenance

nomic_engine.install()
cost_meter.install()

import cognee  # noqa: E402

from estates import estate_for_brain, estate_for_memory, estate_for_vault, label_sections  # noqa: E402

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
CORPUS = STATE / "corpus.jsonl"
WATERMARK = STATE / "shadow_watermark.json"
SOAK_LOG = STATE / "soak.jsonl"
LOCK = STATE / "spine.lock"
QUARANTINE = STATE / "quarantine.jsonl"

BRAIN_DB = "$HOME/.zeroclaw/workspace/memory/brain.db"
BRAIN_EXCLUDED_CATEGORY = "work"  # heading-chunks of vault notes we already carry
MAX_UNITS_PER_PASS = int(os.environ.get("SHADOW_MAX_UNITS", "400"))

# Same §4 routing as the backfill: personal/shared/work cognify (work behind the
# provider approval); work_confidential is ledger-only pointer, never to any LLM.
DATASETS = ("personal", "shared", "work", "work_confidential")
WORK_PROVIDER_APPROVED = os.environ.get("MIMIR_WORK_PROVIDER_APPROVED", "").lower() == "true"
DATA_PER_BATCH = int(os.environ.get("COGNEE_DATA_PER_BATCH", "2"))  # bound to the serial shim


def sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------- plumbing
def db_host_rows(inner_sql: str):
    sql = (f"SELECT translate(encode(convert_to(row_to_json(t)::text,'UTF8'),'base64'),"
           f" E'\\n','') FROM ({inner_sql}) t;")
    inner = ("docker exec -i db-host-db bash -c "
             "'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U db-host -d db-host -qtAX -v ON_ERROR_STOP=1 -f -'")
    argv = (["bash", "-c", inner] if socket.gethostname() == "db-host"
            else ["ssh", "-o", "ConnectTimeout=10", "db-host", inner])
    proc = subprocess.run(argv, input=sql, capture_output=True, text=True, check=True)
    for line in proc.stdout.splitlines():
        if line.strip():
            yield json.loads(base64.b64decode(line).decode("utf-8", "replace"))


def worker_rows(script: str):
    argv = ["ssh", "-o", "ConnectTimeout=10", "worker", "python3 -"]
    proc = subprocess.run(argv, input=script, capture_output=True, text=True, check=True)
    for line in proc.stdout.splitlines():
        if line.strip():
            yield json.loads(line)


def default_watermarks() -> dict:
    """The moment the backfill corpus was snapshotted. Anything written after that
    instant is, by definition, not in the backfill.

    Each source keeps its watermark in ITS OWN timestamp format, because the tailers
    compare against it in that source's own dialect:
      * memory / vault -> Postgres timestamptz, offset-aware
      * brain.db       -> a naive ISO TEXT column ('2026-07-09T12:04:19.740600'),
                          compared as a STRING by sqlite. Feeding it an offset-suffixed
                          value would make the comparison lexicographic nonsense.
    """
    ts = CORPUS.stat().st_mtime if CORPUS.exists() else time.time()
    aware = datetime.fromtimestamp(ts, tz=timezone.utc)
    return {
        "memory": aware.isoformat(),
        "vault": aware.isoformat(),
        "brain": aware.replace(tzinfo=None).isoformat(),
    }


def load_watermark() -> dict:
    if WATERMARK.exists():
        return json.loads(WATERMARK.read_text())
    return default_watermarks()


def save_watermark(wm: dict) -> None:
    tmp = WATERMARK.with_suffix(".tmp")
    tmp.write_text(json.dumps(wm, indent=2))
    os.replace(tmp, WATERMARK)  # atomic: a crash never leaves a truncated watermark


# ---------------------------------------------------------------------- sources
def tail_memory(since: str):
    sql = f"""
      SELECT content, namespace, source,
             to_char(GREATEST(created_at, updated_at) AT TIME ZONE 'UTC',
                     'YYYY-MM-DD"T"HH24:MI:SS.USOF') AS ts
        FROM memory.entries
       WHERE GREATEST(created_at, updated_at) > '{since}'::timestamptz
         AND length(trim(content)) > 0
       ORDER BY GREATEST(created_at, updated_at)
       LIMIT {MAX_UNITS_PER_PASS}
    """
    for r in db_host_rows(sql):
        estate, ambiguous = estate_for_memory(r["namespace"], r["content"])
        yield "memory", r["ts"], {
            "source": "memory",
            "provenance": f"memory:{r['namespace']}/{r['source']}",
            "title": (r["content"].strip().splitlines() or [""])[0][:120],
            "text": r["content"], "estate": estate, "ambiguous": ambiguous,
        }


def tail_vault(since: str):
    sql = f"""
      SELECT relative_path, title, content,
             to_char(indexed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.USOF') AS ts
        FROM raw.notes_index
       WHERE removed_at IS NULL AND indexed_at > '{since}'::timestamptz
         AND content IS NOT NULL AND length(trim(content)) > 0
       ORDER BY indexed_at
       LIMIT {MAX_UNITS_PER_PASS}
    """
    for r in db_host_rows(sql):
        estate, ambiguous = estate_for_vault(r["relative_path"], r["content"])
        yield "vault", r["ts"], {
            "source": "vault",
            "provenance": f"vault:{r['relative_path']}",
            "title": r["title"] or r["relative_path"],
            "text": r["content"], "estate": estate, "ambiguous": ambiguous,
        }


def tail_brain(since: str):
    script = f"""
import sqlite3, json, sys
c = sqlite3.connect("file:{BRAIN_DB}?mode=ro", uri=True)
q = ("SELECT key, category, content, updated_at FROM memories "
     "WHERE category != '{BRAIN_EXCLUDED_CATEGORY}' AND updated_at > ? "
     "ORDER BY updated_at LIMIT {MAX_UNITS_PER_PASS}")
for key, cat, content, ts in c.execute(q, ({since!r},)):
    if content and content.strip():
        sys.stdout.write(json.dumps(
            {{"key": key, "category": cat, "content": content, "ts": ts}}) + "\\n")
"""
    for r in worker_rows(script):
        estate, ambiguous = estate_for_brain(r["key"], r["category"], r["content"])
        yield "brain", r["ts"], {
            "source": "brain",
            "provenance": f"brain:{r['key'][:160]}",
            "title": r["key"][:120],
            "text": r["content"], "estate": estate, "ambiguous": ambiguous,
        }


def quarantine(doc, reason: str) -> None:
    with QUARANTINE.open("a") as fh:
        fh.write(json.dumps({"sha256": doc["sha256"], "dataset": doc["_prov"]["dataset"],
                             "provenance": doc["provenance"], "reason": reason,
                             "stage": "shadow"}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


async def _cognify_dataset(dataset: str, docs: list) -> tuple[list, int]:
    """Cognify a dataset's units; return (placed, quarantined). Isolates a poison doc."""
    quarantined = 0
    try:
        for d in docs:
            await cognee.add(provenance.as_document(d["title"], d["text"], d["_prov"]),
                             dataset_name=dataset)
        await cognee.cognify(datasets=[dataset], incremental_loading=True, data_per_batch=DATA_PER_BATCH)
        return docs, 0
    except Exception as e:  # noqa: BLE001
        print(f"  [{dataset}] batch failed ({type(e).__name__}) — isolating", flush=True)
        placed = []
        for d in docs:
            try:
                await cognee.add(provenance.as_document(d["title"], d["text"], d["_prov"]),
                                 dataset_name=dataset)
                await cognee.cognify(datasets=[dataset], incremental_loading=True, data_per_batch=DATA_PER_BATCH)
                placed.append(d)
            except Exception as inner:  # noqa: BLE001
                quarantine(d, f"{type(inner).__name__}: {str(inner)[:200]}")
                quarantined += 1
        return placed, quarantined


# ------------------------------------------------------------------------- main
async def run_pass(dry_run: bool) -> int:
    wm = load_watermark()
    print(f"watermarks: {json.dumps(wm)}")

    units, high = [], dict(wm)
    for name, tail in (("memory", tail_memory), ("vault", tail_vault), ("brain", tail_brain)):
        try:
            for src, ts, doc in tail(wm[name]):
                # A mixed document is split by section and each section labelled, exactly
                # as in the backfill — otherwise a Daily note written during the soak
                # would put its client section into personal recall. Then attach the §4
                # provenance tuple (which derives the storage dataset) per section.
                for sec in label_sections(doc["text"], doc["estate"], doc["ambiguous"]):
                    prov = provenance.build_provenance(doc["provenance"], sec.estate, sec.text)
                    units.append({**doc, "text": sec.text, "estate": sec.estate,
                                  "title": sec.heading or doc["title"],
                                  "sha256": sha(sec.text), "_prov": prov})
                high[name] = max(high[name], ts)
        except subprocess.CalledProcessError as e:
            print(f"  [{name}] SOURCE UNREACHABLE: {e.stderr[:120]}", file=sys.stderr)
            return 1  # fail loud; do not advance any watermark on a partial read

    by_dataset = {}
    for u in units:
        by_dataset.setdefault(u["_prov"]["dataset"], []).append(u)

    print(f"new units: {len(units)}  " +
          "  ".join(f"{k}={len(by_dataset[k])}" for k in DATASETS if by_dataset.get(k)))
    if dry_run:
        for u in units[:10]:
            print(f"  [{u['_prov']['dataset']:<18}] {u['provenance'][:80]}")
        print("(dry run — nothing written)")
        return 0
    if not units:
        save_watermark(high)
        return 0

    placed_total, quarantined, deferred = 0, 0, 0
    for dataset in DATASETS:
        docs = by_dataset.get(dataset)
        if not docs:
            continue
        if dataset == "work_confidential":
            # ledger-only pointer: register, never cognify (no content to any LLM).
            ledger.record(docs, stage="shadow")
            placed_total += len(docs)
            print(f"  [work_confidential] {len(docs)} REGISTERED pointer-only", flush=True)
            continue
        if dataset == "work" and not WORK_PROVIDER_APPROVED:
            # Hold these: do not place, and (below) do not advance the watermark past
            # them, so they are re-read once a provider is approved.
            deferred += len(docs)
            print(f"  [work] {len(docs)} DEFERRED (no approved provider)")
            continue
        placed, q = await _cognify_dataset(dataset, docs)
        ledger.record(placed, stage="shadow")
        placed_total += len(placed)
        quarantined += q

    # Advance the watermark ONLY if nothing was deferred. A per-source watermark spans
    # all datasets, so advancing it while work units were held would lose them forever.
    # With no deferrals, advance — a crash re-reads, which cognee dedup + idempotent
    # ledger upsert make free.
    if deferred:
        print(f"  watermark HELD: {deferred} work units deferred pending a provider "
              f"(re-read next pass)")
    else:
        save_watermark(high)

    c = cost_meter.snapshot()
    entry = {"at": datetime.now(timezone.utc).isoformat(), "units": len(units),
             "placed": placed_total, "deferred": deferred,
             "by_dataset": {k: len(v) for k, v in by_dataset.items()},
             "quarantined": quarantined, "usd": round(c["usd"], 4), "llm_calls": c["calls"],
             "watermarks": high if not deferred else wm}
    with SOAK_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"shadowed {placed_total}/{len(units)} units, ${c['usd']:.4f}, "
          f"quarantined {quarantined}, deferred {deferred}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would be shadowed")
    args = ap.parse_args()

    if os.environ.get("ENABLE_BACKEND_ACCESS_CONTROL", "").lower() != "true":
        print("REFUSING: ENABLE_BACKEND_ACCESS_CONTROL must be True.", file=sys.stderr)
        return 2
    if not args.dry_run and not os.environ.get("SKIP_EMBED_SPACE_CHECK"):
        import check_embed_space
        if check_embed_space.main() != 0:
            return 2

    # cognee cognifies a whole dataset, so a shadow pass and the backfill must never
    # touch the same dataset concurrently.
    STATE.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        return asyncio.run(run_pass(True))  # reads only; no lock needed

    if subprocess.run(["pgrep", "-f", "backfill.py"], capture_output=True).returncode == 0:
        print("backfill is running — skipping this shadow pass")
        return 0
    with LOCK.open("w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another spine writer holds the lock — skipping")
            return 0
        return asyncio.run(run_pass(False))


if __name__ == "__main__":
    sys.exit(main())
