#!/usr/bin/env python
"""Stage D — parity: does the cognee spine recall at least as well as the old stores?

This decides the cutover go/no-go, so the bar is fixed HERE, before any numbers are
seen, and the query set is held out from anything used to tune the migration.

Contenders, all asked the same held-out questions:
  OLD-memory   memory.hybrid_search()  (40% text + 60% vector cosine, nomic-768)
  OLD-search   search.hybrid_search()  (search.embeddings, MiniLM-384)
  NEW-cognee   GRAPH_COMPLETION scoped to the question's estate

Scoring reuses the phase-1 bake-off's method so the numbers are comparable: an LLM
judge with ground truth, 0/1/2 per query, one standard across all systems. It is
NOT self-evaluation by the system under test — the judge sees only the answers.

PRE-COMMITTED DECISION RULE (spec 05 acceptance: "cognee recall >= old"):
  GO   iff  mean(NEW) >= mean(best OLD)  on the held-out set
       AND  zero cross-estate leaks in the estate probes
       AND  the pgvector floor still answers every query it answered before
  Any leak is an automatic NO-GO regardless of relevance.

  ./with_env_prod.sh ./venv/bin/python parity.py --queries parity_queries.json
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import nomic_engine

nomic_engine.install()

import cognee  # noqa: E402
from cognee.modules.search.types import SearchType  # noqa: E402

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
OUT = STATE / "parity_results.json"

JUDGE_MODEL = os.environ.get("PARITY_JUDGE_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------- old stores
def db_host_rows(sql: str, wrap_txn: bool = False):
    inner = (f"SELECT translate(encode(convert_to(row_to_json(t)::text,'UTF8'),'base64'),"
             f" E'\\n','') FROM ({sql}) t;")
    if wrap_txn:
        inner = f"BEGIN;\n{inner}\nROLLBACK;"
    cmd = ("docker exec -i db-host-db bash -c "
           "'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U db-host -d db-host -qtAX -v ON_ERROR_STOP=1 -f -'")
    proc = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "db-host", cmd],
                          input=inner, capture_output=True, text=True, check=True)
    return [json.loads(base64.b64decode(l).decode("utf-8", "replace"))
            for l in proc.stdout.splitlines() if l.strip()]


def db_host_rows_txn(sql: str):
    return db_host_rows(sql, wrap_txn=True)


def embed_768(text: str):
    """Query embedding for memory.hybrid_search, from the same nomic model."""
    import urllib.request
    endpoint = os.environ["EMBEDDING_ENDPOINT"]
    body = json.dumps({"model": "nomic-embed-text:latest", "input": text}).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["embeddings"][0]


def old_memory_recall(query: str, k: int = 5):
    """memory.hybrid_search at full strength (it returns 0 rows without a vector).

    The function UPDATEs retrieval_count/last_retrieved_at as a side effect, so the
    call is wrapped in a rolled-back transaction: the parity run must not mutate
    production memory, not even its statistics.
    """
    vec = "[" + ",".join(f"{x:.6f}" for x in embed_768(query)) + "]"
    q = query.replace("'", "''")
    rows = db_host_rows_txn(
        f"SELECT content FROM memory.hybrid_search("
        f"p_query := '{q}', p_embedding := '{vec}'::vector, p_limit := {k})"
    )
    return [r["content"] for r in rows]


def old_search_recall(query: str, k: int = 5):
    """The LIVE Synapse search API (worker:8300), which embeds the query with
    all-MiniLM-L6-v2 and passes the vector to search.hybrid_search.

    Calling search.hybrid_search with a NULL vector (as the owner's iOS app MCP tool does)
    would be text-only and would handicap the incumbent. The decision rule is
    "cognee >= best OLD", so the incumbent is measured at full strength.
    """
    import urllib.request
    key = os.environ.get("SEARCH_KEY", "")
    body = json.dumps({"query": query, "limit": k}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Search-Key"] = key
    req = urllib.request.Request("http://localhost:8300/v1/search",
                                 data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read())
    return [x.get("content", "") for x in payload.get("results", [])]


# ---------------------------------------------------------------- new spine
def unwrap(result, expected_dataset):
    items = result if isinstance(result, list) else [result]
    parts = []
    for item in items:
        if isinstance(item, dict) and "search_result" in item:
            if item.get("dataset_name") not in (None, expected_dataset):
                raise AssertionError(
                    f"cognee returned dataset {item.get('dataset_name')!r} for a query "
                    f"scoped to {expected_dataset!r}")
            parts.append(str(item["search_result"]))
        else:
            parts.append(str(item))
    return "\n".join(parts)


async def cognee_recall(query: str, estate: str, k: int = 5) -> str:
    # `shared` is queried alongside the estate, never across estates.
    datasets = [estate] if estate == "shared" else [estate, "shared"]
    datasets = [d for d in datasets if d in await _existing_datasets()]
    out = []
    for ds in datasets:
        r = await cognee.search(query_text=query, query_type=SearchType.GRAPH_COMPLETION,
                                datasets=[ds], top_k=k)
        out.append(unwrap(r, ds))
    return "\n".join(out)


_datasets_cache = None


async def _existing_datasets():
    global _datasets_cache
    if _datasets_cache is None:
        from cognee.modules.data.methods import get_datasets
        from cognee.modules.users.methods import get_default_user
        user = await get_default_user()
        _datasets_cache = {d.name for d in await get_datasets(user.id)}
    return _datasets_cache


# ---------------------------------------------------------------- judge
JUDGE_SYSTEM = (
    "You grade a retrieval system's answer against ground truth. Output ONLY a "
    "single digit.\n"
    "2 = the answer contains all the ground-truth facts.\n"
    "1 = it contains some but not all, or is correct but incomplete.\n"
    "0 = it contains none of them, is wrong, or says it does not know.\n"
    "Ignore style, length, and any surrounding metadata. Grade only the facts."
)


def judge(query: str, ground_truth: str, answer: str) -> int:
    import urllib.error
    import urllib.request
    key = os.environ["OPENROUTER_API_KEY"]
    user = (f"QUESTION: {query}\n\nGROUND TRUTH: {ground_truth}\n\n"
            f"SYSTEM ANSWER:\n{answer[:6000]}\n\nScore (0, 1, or 2):")
    body = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": 8,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        txt = json.loads(r.read())["choices"][0]["message"]["content"]
    for ch in txt:
        if ch in "012":
            return int(ch)
    return 0


# ---------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(HERE / "parity_queries.json"))
    args = ap.parse_args()

    qpath = Path(args.queries)
    if not qpath.exists():
        print(f"REFUSING: no query set at {qpath}.\n"
              f"Draft one with gen_parity_queries.py, have the owner verify each ground\n"
              f"truth against its _excerpt, then rename to parity_queries.json.",
              file=sys.stderr)
        return 2
    queries = json.loads(qpath.read_text())

    # The go/no-go rests on this set. Ground truth an LLM merely drafted is not ground
    # truth. Every query must carry `_verified` AND a `_supporting_quote` that
    # verify_parity_queries.py confirmed occurs verbatim in the source document.
    unverified = [q["id"] for q in queries
                  if not q.get("_verified") or not q.get("_supporting_quote")]
    if unverified:
        print(f"REFUSING: {len(unverified)} queries are unverified "
              f"({', '.join(unverified[:6])}{'...' if len(unverified) > 6 else ''}).\n"
              f"Run verify_parity_queries.py, or set _verified + _supporting_quote by hand.",
              file=sys.stderr)
        return 2

    by = {q.get("_verified_by", "?") for q in queries}
    print(f"query set verified by: {', '.join(sorted(by))}")
    if not any("owner" in b.lower() and "delegated" not in b.lower() for b in by):
        print("NOTE: this set was verified by delegation, not by the owner directly.")

    results = []
    leaks = []

    for q in queries:
        row = {"id": q["id"], "estate": q["estate"], "class": q.get("class", "-"),
               "query": q["query"], "ground_truth": q["ground_truth"]}

        try:
            row["old_memory"] = "\n".join(old_memory_recall(q["query"]))
        except Exception as e:  # noqa: BLE001
            row["old_memory"] = f"ERROR {type(e).__name__}: {str(e)[:120]}"
        try:
            row["old_search"] = "\n".join(old_search_recall(q["query"]))
        except Exception as e:  # noqa: BLE001
            row["old_search"] = f"ERROR {type(e).__name__}: {str(e)[:120]}"
        try:
            row["cognee"] = await cognee_recall(q["query"], q["estate"])
        except Exception as e:  # noqa: BLE001
            row["cognee"] = f"ERROR {type(e).__name__}: {str(e)[:120]}"

        for system in ("old_memory", "old_search", "cognee"):
            row[f"score_{system}"] = judge(q["query"], q["ground_truth"], row[system])

        # estate leak probe: forbidden markers must never appear in the answer
        for marker in q.get("forbidden_markers", []):
            if marker.lower() in row["cognee"].lower():
                leaks.append({"id": q["id"], "marker": marker, "estate": q["estate"]})

        print(f"  {q['id']:<5} {q['estate']:<9} "
              f"mem={row['score_old_memory']} search={row['score_old_search']} "
              f"cognee={row['score_cognee']}", flush=True)
        results.append(row)

    STATE.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"results": results, "leaks": leaks}, indent=2,
                              ensure_ascii=False))

    # ---- verdict ----
    def mean(sys_):
        vals = [r[f"score_{sys_}"] for r in results]
        return sum(vals) / len(vals) if vals else 0.0

    by_class = defaultdict(lambda: defaultdict(list))
    for r in results:
        for s in ("old_memory", "old_search", "cognee"):
            by_class[r["class"]][s].append(r[f"score_{s}"])

    print(f"\n=== parity ({len(results)} held-out queries, 0-2 scale) ===")
    print(f"{'class':<14}{'OLD-memory':>12}{'OLD-search':>12}{'NEW-cognee':>12}")
    for cls, d in sorted(by_class.items()):
        print(f"{cls:<14}"
              f"{sum(d['old_memory'])/len(d['old_memory']):>12.2f}"
              f"{sum(d['old_search'])/len(d['old_search']):>12.2f}"
              f"{sum(d['cognee'])/len(d['cognee']):>12.2f}")
    m_old_mem, m_old_search, m_new = mean("old_memory"), mean("old_search"), mean("cognee")
    best_old = max(m_old_mem, m_old_search)
    print(f"{'ALL':<14}{m_old_mem:>12.2f}{m_old_search:>12.2f}{m_new:>12.2f}")

    print(f"\nbest OLD = {best_old:.2f}   NEW = {m_new:.2f}   "
          f"delta = {(m_new - best_old):+.2f}")
    print(f"cross-estate leaks: {len(leaks)}")
    for lk in leaks:
        print(f"  LEAK {lk['id']}: {lk['marker']!r} surfaced in {lk['estate']} recall")

    go = (m_new >= best_old) and not leaks
    print(f"\nVERDICT: {'GO (recall >= old, no leaks)' if go else 'NO-GO'}")
    print("This is advisory input to the owner's go/no-go. No reads are cut over here.")
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
