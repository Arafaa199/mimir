#!/usr/bin/env python
"""Stage B step 2 — backfill the corpus into the cognee spine. NON-DESTRUCTIVE.

Reads `.state/corpus.jsonl` (built by extract_corpus.py). Adds each document to the
cognee dataset named after its estate (`personal` / `work` / `shared`), then runs
cognify per estate. Never reads or writes memory.entries, brain.db or
search.embeddings — the old stores are untouched and stay authoritative until
cutover.

RESUMABLE. Every completed document's sha256 is appended to `.state/backfilled.jsonl`
before the next batch starts, so an interrupted 6-hour run (laptop sleeps, roams off
LAN, OpenRouter 429s) resumes without re-paying for the same LLM extraction.

  ./with_env_prod.sh ./venv/bin/python backfill.py --limit 500      # pilot, ~$1
  ./with_env_prod.sh ./venv/bin/python backfill.py                  # full run
  ./with_env_prod.sh ./venv/bin/python backfill.py --estate work    # one estate

ENABLE_BACKEND_ACCESS_CONTROL must be True (with_env_prod.sh defaults it) or the
estates share one store — see probe_estate_leak.py.
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import cost_meter
import ledger
import nomic_engine
import provenance

nomic_engine.install()
cost_meter.install()

import cognee  # noqa: E402

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
CORPUS = STATE / "corpus.jsonl"
DONE = STATE / "backfilled.jsonl"
QUARANTINE = STATE / "quarantine.jsonl"
LOCK = STATE / "spine.lock"

DATASETS = ("personal", "shared", "work", "work_confidential")
# cognee processes up to `data_per_batch` docs CONCURRENTLY (default 20). The claude
# shim is serial (concurrency=1, auth-safe), so 20 concurrent docs flood its queue and
# the tail request times out at litellm's 600s. Throughput is shim-bound regardless, so a
# small value just keeps the queue shallow. 2 for claude; raise for a concurrent backend.
DATA_PER_BATCH = int(os.environ.get("COGNEE_DATA_PER_BATCH", "2"))

# §4 confidential + provider boundary. `work` (normal) content is shipped chunk-by-chunk
# to the cognify LLM; §21 forbids work/confidential context reaching a free/logging
# provider. Until the owner rules on an approved provider for work, only personal/shared
# cognify (they may use OpenRouter). `work_confidential` never ships content at all —
# it is registered pointer-only in the ledger, no LLM — so it is always safe to run.
WORK_PROVIDER_APPROVED = os.environ.get("MIMIR_WORK_PROVIDER_APPROVED", "").lower() == "true"
OPEN_DATASETS = ("personal", "shared")  # always cognify-able on the current provider


def load_corpus():
    with CORPUS.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_done() -> set[str]:
    if not DONE.exists():
        return set()
    done = set()
    with DONE.open() as fh:
        for line in fh:
            if line.strip():
                done.add(json.loads(line)["sha256"])
    return done


def mark_done(docs):
    """Commit a batch: write the §4 provenance ledger, THEN the append-only checkpoint.

    Ledger first: if we crash between the two, the unit re-runs (checkpoint absent) and
    the ledger row is idempotently rewritten — safe. The reverse order could checkpoint a
    unit whose provenance never landed, and provenance cannot be retrofitted."""
    ledger.record(docs, stage="backfill")
    with DONE.open("a") as fh:
        for d in docs:
            fh.write(json.dumps({"sha256": d["sha256"], "dataset": d["_prov"]["dataset"],
                                 "provenance": d["provenance"]}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def as_document(doc) -> str:
    """cognee input, provenance-aware. Confidential units contribute only a pointer stub
    (their body never reaches an LLM); everything else carries its §4 header."""
    return provenance.as_document(doc["title"], doc["text"], doc["_prov"])


def quarantine(doc, reason: str) -> None:
    """A document cognee refuses to ingest is recorded, never dropped.

    Gemini's content filter rejects some of this corpus outright (an HTB exploit
    write-up killed a whole batch). Silently skipping such a document would delete a
    memory the owner still has in the old store, and nobody would ever know which.
    """
    with QUARANTINE.open("a") as fh:
        fh.write(json.dumps({"sha256": doc["sha256"], "dataset": doc["_prov"]["dataset"],
                             "provenance": doc["provenance"], "chars": doc["chars"],
                             "reason": reason}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def register_pointers(docs: list) -> dict:
    """work_confidential: LEDGER-ONLY, no LLM (spec 05 §4 confidential tier).

    Confidential content is never cognified in v1 — not even a pointer stub, because
    cognify would ship the stub (which names the document) to a provider, and §20 pins
    confidential cognify to an approved provider we do not yet have. The unit is
    REGISTERED in the ledger (provable, purgeable) and its body stays only in the old
    store. Recall over the confidential scope reads the ledger and returns "retrieve the
    source directly", never content.
    """
    mark_done(docs)  # ledger row (pointer_only=true) + checkpoint; no cognee call
    print(f"  [work_confidential] {len(docs)} units REGISTERED pointer-only "
          f"(no content to any LLM)", flush=True)
    return {"docs": len(docs), "chars": 0, "batches": 0, "seconds": 0.0,
            "quarantined": 0, "pointer_only": True}


async def _cognify_one(doc, dataset: str) -> str:
    """Ingest a single document. Returns 'ok', 'transient', or 'poison'.

    'transient' (an infra blip — network unreachable during a reboot, a 429) is NOT
    quarantined: the doc is left unmarked so it retries on the next run. Quarantining a
    transient error is exactly what branded 4,456 good docs as failures during a
    post-reboot network blip. Only a genuine per-doc failure (unsatisfiable schema, content
    a provider refuses) is isolated as 'poison'."""
    try:
        await cognee.add(as_document(doc), dataset_name=dataset)
        await cognee.cognify(datasets=[dataset], incremental_loading=True, data_per_batch=DATA_PER_BATCH)
        return "ok"
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            print(f"    SKIP transient {doc['provenance'][:60]} -> {type(e).__name__}", flush=True)
            return "transient"
        quarantine(doc, f"{type(e).__name__}: {str(e)[:200]}")
        print(f"    QUARANTINED {doc['provenance'][:70]} -> {type(e).__name__}", flush=True)
        return "poison"


class BudgetExhausted(Exception):
    """Metered spend hit the cap. Raised between batches, never mid-cognify."""


BATCH_RETRIES = int(os.environ.get("BACKFILL_BATCH_RETRIES", "4"))
# Infra hiccups to RETRY, never poison to isolate. The network markers matter for the
# reboot case specifically: right after a power-loss reboot the host's routes/tailnet are
# not up for a few seconds, so a connection to the shim / ollama / db fails with
# `OSError: [Errno 101] Network is unreachable` (and kin). Before these were here one such
# blip made _is_transient() return False, the batch skipped straight to per-doc isolation,
# and 4,456 perfectly good docs were quarantined at 0s each. Now it backs off and retries.
_TRANSIENT_MARKERS = ("timeout", "timed out", "429", "rate_limit", "rate limit",
                      "temporarily", "connection reset", "connection aborted",
                      "service unavailable", "502", "503", "504", "overloaded",
                      "instructorretry",
                      "unreachable", "no route to host", "network is down",
                      "connection refused", "broken pipe", "errno 101", "errno 111",
                      "errno 113", "name or service not known",
                      "temporary failure in name resolution")


# How many consecutive batches may land ZERO documents on transient errors before we
# conclude the backend is gone rather than flaky. 3 batches x BATCH_RETRIES of backoff is
# minutes of hard evidence, not a blip.
DEAD_BACKEND_BATCHES = 3


class BackendDown(Exception):
    """The LLM/embedding backend has gone away mid-run.

    This is NOT a document problem, so it must not be handled like one. Both endpoints
    (claude-shim :8088, ollama :11434) live on worker -- a laptop that sleeps. When it
    naps mid-run, EVERY remaining doc fails transient, the run skips all 5,451 of them,
    exits 0, and systemd's Restart=on-failure never fires because nothing "failed".
    That is precisely how the migration sat at 25/5451 for a day with nobody told.

    Raising this aborts the run with a non-zero exit so the unit restarts and
    wait_for_net() blocks until the backend is genuinely back.
    """


def _is_transient(exc: Exception) -> bool:
    """A provider hiccup we should retry, vs a poison document we should isolate."""
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(m in blob for m in _TRANSIENT_MARKERS)


async def backfill_dataset(dataset: str, docs: list, batch_size: int,
                           max_usd: float | None = None) -> dict:
    stats = {"docs": 0, "chars": 0, "batches": 0, "seconds": 0.0, "quarantined": 0,
             "transient": 0}
    dead_batches = 0
    for i in range(0, len(docs), batch_size):
        # Checked between batches: the checkpoint is consistent here, so stopping is
        # free and resuming re-does nothing. This job runs unattended for ~16h.
        if max_usd and cost_meter.snapshot()["usd"] >= max_usd:
            raise BudgetExhausted(f"spend ${cost_meter.snapshot()['usd']:.2f} "
                                  f">= cap ${max_usd:.2f}")
        batch = docs[i:i + batch_size]
        t0 = time.time()
        ok = None
        # A transient provider failure (OpenRouter 429, a socket timeout) must never
        # kill an unattended multi-hour run. Retry the whole batch with backoff before
        # concluding a document is actually poison.
        for attempt in range(BATCH_RETRIES):
            try:
                for doc in batch:
                    await cognee.add(as_document(doc), dataset_name=dataset)
                # incremental_loading skips documents this pipeline already processed
                # (keyed on a content hash), so re-cognifying the dataset once per batch
                # costs one cheap lookup per prior doc, not another LLM extraction.
                await cognee.cognify(datasets=[dataset], incremental_loading=True, data_per_batch=DATA_PER_BATCH)
                ok = batch
                break
            except Exception as e:  # noqa: BLE001
                transient = _is_transient(e)
                if transient and attempt < BATCH_RETRIES - 1:
                    wait = 15 * (attempt + 1)
                    print(f"  [{dataset}] batch {i//batch_size + 1} transient "
                          f"({type(e).__name__}: {str(e)[:80]}) — retry in {wait}s",
                          flush=True)
                    await asyncio.sleep(wait)
                    continue
                # Non-transient, or retries exhausted: one poison document fails the whole
                # batch (cognee cognifies the dataset, not the item). Isolate per document
                # so the rest still land and the bad one is named rather than lost.
                print(f"  [{dataset}] batch {i//batch_size + 1} failed "
                      f"({type(e).__name__}: {str(e)[:100]}) — isolating per document",
                      flush=True)
                ok = []
                batch_transient = 0
                for doc in batch:
                    status = await _cognify_one(doc, dataset)
                    if status == "ok":
                        ok.append(doc)
                    elif status == "poison":
                        stats["quarantined"] += 1
                    else:
                        # 'transient': leave unmarked — not done, not quarantined —
                        # it retries on the next run.
                        batch_transient += 1
                stats["transient"] += batch_transient

                # Circuit breaker. A batch that lands NOTHING and was entirely transient
                # is not a flaky provider, it is a missing one. Three in a row = the
                # backend is down; stop rather than skipping the whole corpus and
                # exiting 0.
                if not ok and batch_transient:
                    dead_batches += 1
                    if dead_batches >= DEAD_BACKEND_BATCHES:
                        raise BackendDown(
                            f"{dead_batches} consecutive batches landed 0 docs, all "
                            f"transient ({stats['transient']} skipped). The backend "
                            f"(claude-shim / ollama on worker) is unreachable. Stopping "
                            f"so the checkpoint stays clean and the unit restarts."
                        )
                else:
                    dead_batches = 0
                break

        mark_done(ok)
        elapsed = time.time() - t0
        chars = sum(d["chars"] for d in ok)
        stats["docs"] += len(ok)
        stats["chars"] += chars
        stats["batches"] += 1
        stats["seconds"] += elapsed
        c = cost_meter.snapshot()
        print(f"  [{dataset}] {stats['docs']}/{len(docs)} docs  "
              f"{stats['chars']/(1024*1024):.2f} MB  {elapsed:.0f}s/batch  "
              f"spent ${c['usd']:.3f} ({c['calls']} llm calls)"
              + (f"  quarantined={stats['quarantined']}" if stats["quarantined"] else ""),
              flush=True)
    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="pilot: sample this many docs per dataset")
    ap.add_argument("--dataset", choices=DATASETS, help="restrict to one dataset")
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--max-usd", type=float,
                    default=float(os.environ.get("BACKFILL_MAX_USD", "0")) or None,
                    help="stop cleanly once metered LLM spend reaches this (unattended runs)")
    args = ap.parse_args()

    if os.environ.get("ENABLE_BACKEND_ACCESS_CONTROL", "").lower() != "true":
        print("REFUSING: ENABLE_BACKEND_ACCESS_CONTROL must be True or estates share "
              "one store (see probe_estate_leak.py).", file=sys.stderr)
        return 2

    # Hard precondition. Embedding a corpus from the wrong ollama build produces a
    # store whose vectors sit ~0.87 from every query production later issues, and
    # nothing downstream would notice: within one run it is all self-consistent.
    if not os.environ.get("SKIP_EMBED_SPACE_CHECK"):
        import check_embed_space
        if check_embed_space.main() != 0:
            print("REFUSING: embedding endpoint is not production's space.",
                  file=sys.stderr)
            return 2

    # Exclusive across spine writers: cognify operates on a whole dataset, so the
    # backfill and a shadow pass must never run against the same dataset at once.
    STATE.mkdir(parents=True, exist_ok=True)
    lk = LOCK.open("w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another spine writer holds the lock — refusing", file=sys.stderr)
        return 2

    corpus = load_corpus()
    done = load_done()
    # §4 provenance is attached to EVERY unit here — source, source_trust, estate,
    # sensitivity, and the derived dataset. This is the day-one invariant; a unit
    # without it must never reach the spine.
    for d in corpus:
        d["_prov"] = provenance.build_provenance(d["provenance"], d["estate"], d["text"])
    pending = [d for d in corpus if d["sha256"] not in done]
    if args.dataset:
        pending = [d for d in pending if d["_prov"]["dataset"] == args.dataset]

    by_dataset = defaultdict(list)
    for d in pending:
        by_dataset[d["_prov"]["dataset"]].append(d)

    if args.limit:
        # Systematic sample across the SIZE distribution of each dataset: sort by
        # length, then take evenly spaced items, so the mean doc size stays
        # representative and the cost extrapolation is honest.
        per = max(1, args.limit // max(1, len(by_dataset)))
        for k in list(by_dataset):
            ordered = sorted(by_dataset[k], key=lambda d: d["chars"])
            if len(ordered) > per:
                step = len(ordered) / per
                ordered = [ordered[int(i * step)] for i in range(per)]
            by_dataset[k] = ordered

    total_docs = sum(len(v) for v in by_dataset.values())
    total_mb = sum(d["chars"] for v in by_dataset.values() for d in v) / (1024 * 1024)
    print(f"corpus={len(corpus)} already_done={len(done)} pending={len(pending)}")
    print("by dataset: " + "  ".join(f"{k}={len(by_dataset[k])}" for k in DATASETS if by_dataset.get(k)))
    if not WORK_PROVIDER_APPROVED and by_dataset.get("work"):
        print(f"NOTE: {len(by_dataset['work'])} work units DEFERRED — cognify ships content "
              f"to the LLM and §21 forbids work context on a free/logging provider; set "
              f"MIMIR_WORK_PROVIDER_APPROVED=true once the owner rules on the provider.")
    print()
    if not total_docs:
        print("nothing to do")
        return 0

    if args.max_usd:
        print(f"spend cap: ${args.max_usd:.2f} (checked between batches)\n")

    t0 = time.time()
    results = {}
    stopped = None
    for dataset in DATASETS:
        docs = by_dataset.get(dataset)
        if not docs:
            continue
        # work_confidential: ledger-only, never cognified (no content to any LLM).
        if dataset == "work_confidential":
            results[dataset] = register_pointers(docs)
            continue
        # normal work: content goes to the cognify LLM — gated on the provider ruling.
        if dataset == "work" and not WORK_PROVIDER_APPROVED:
            print(f"[work] {len(docs)} units SKIPPED (no approved provider yet)")
            continue
        print(f"[{dataset}] {len(docs)} docs")
        try:
            results[dataset] = await backfill_dataset(dataset, docs, args.batch_size,
                                                      args.max_usd)
        except BudgetExhausted as e:
            stopped = str(e)
            print(f"\n*** STOPPING: {e}", flush=True)
            break

    wall = time.time() - t0
    mb = sum(r["chars"] for r in results.values()) / (1024 * 1024)
    done_docs = sum(r["docs"] for r in results.values())
    quarantined = sum(r["quarantined"] for r in results.values())
    remaining = len(pending) - done_docs
    print(f"\n=== backfill run complete ===")
    print(f"{'dataset':<18}{'docs':>8}{'MB':>9}{'seconds':>10}{'quarantined':>13}")
    for name, r in results.items():
        note = "  (pointer-only, ledger)" if r.get("pointer_only") else ""
        print(f"{name:<18}{r['docs']:>8}{r['chars']/(1024*1024):>9.2f}"
              f"{r['seconds']:>10.0f}{r['quarantined']:>13}{note}")

    cost_meter.report(done_docs, STATE / "cost.json")
    print(f"\nwall {wall/60:.1f} min   {mb:.2f} MB   {wall/max(1,done_docs):.1f} s/doc")

    # Project on CHUNKS, not bytes: cognee's ECL cost tracks chunk count.
    if remaining and done_docs:
        c = cost_meter.snapshot()
        print(f"remaining: {remaining} docs  "
              f"~{remaining * wall / done_docs / 3600:.1f} h  "
              f"~${remaining * c['usd'] / done_docs:.2f}")
    if quarantined:
        print(f"\n*** {quarantined} document(s) QUARANTINED -> {QUARANTINE}")
        print("    These are NOT in the spine. They remain in the old stores.")
    if stopped:
        print(f"\n*** spend cap reached; {remaining} units still pending. "
              f"Re-run to continue (checkpoint is consistent).")
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except BackendDown as exc:
        # Exit 4: NOT in the unit's SuccessExitStatus, so Restart=on-failure fires and
        # OnFailure= pages. wait_for_net() then blocks the restart until the backend is
        # genuinely back, so this becomes a patient retry loop instead of a silent
        # 5,451-doc no-op. The checkpoint is untouched: nothing was marked done, nothing
        # was quarantined -- the skipped docs simply retry.
        print(f"\n*** BACKEND DOWN: {exc}", file=sys.stderr, flush=True)
        sys.exit(4)
