"""The pgvector recall FLOOR — `memory.entries` behind the SAME scope rules (spec 07).

Why this file is dangerous, and why it is written the way it is:

`memory.entries` is the OLD store. It is **not partitioned**. Personal notes, work
chunks and NDA'd client material all sit in one table — that lack of a wall is exactly the
leak §4 was written to close (ZeroClaw pushed `[Work/...]` vault chunks straight into it,
and ~1,580 genuinely-work fragments were landing in personal recall).

So the floor cannot simply "also search memory.entries". Every candidate row is CLASSIFIED
before it is allowed out:

  1. `estate_for_memory(namespace, content)` decides personal vs work — on the WHOLE row,
     the same call the backfill used, so the floor and the spine agree by construction.
     Its tie-break returns **work**, which fails CLOSED: a misfiled personal row merely goes
     missing (a bug someone notices), a misfiled work row would leak (a bug nobody notices).
  2. `sensitivity_for(...)` decides confidential. A confidential row NEVER returns content
     from here — the pointer-only guarantee has to hold at the floor too, or the floor is
     simply a way to read the client material that cognify was forbidden to touch.
  3. Anything not in the caller's scope is dropped.

The query is embedded with **nomic on db-host ollama** — production's embedding space
(cos 1.000000; laptop's is 0.826 and would silently return garbage).

`memory.hybrid_search` is deliberately NOT used: it returns 0 rows without a vector and it
MUTATES retrieval stats as a side effect of reading. A recall must not write.
"""
import hashlib
import os
from typing import List

import psycopg2
import requests

from ingress import CONFIDENTIAL, PERSONAL, SHARED, WORK
from passage import Passage
from scope import Scope

# Over-fetch: estates are decided in Python AFTER the vector search, so the top-k by
# distance is not the top-k in scope. Pull a wider candidate set and filter down.
_OVERFETCH = 8

_OLLAMA = os.environ.get("OLLAMA_EMBED_HOST", "http://localhost:11434")
_EMBED_MODEL = "nomic-embed-text:latest"


def _embed(text: str) -> list:
    # 60s was too tight: db-host's 4 cores are shared with the backfill, which saturates
    # ollama, so an embed can queue for a while. A slow embed is not an error -- but a
    # recall that dies because a BACKGROUND job is busy is a broken brain.
    timeout = int(os.environ.get("MIMIR_EMBED_TIMEOUT", "180"))
    r = requests.post(f"{_OLLAMA}/api/embed",
                      json={"model": _EMBED_MODEL, "input": text}, timeout=timeout)
    r.raise_for_status()
    vec = r.json()["embeddings"][0]
    if len(vec) != 768:
        raise RuntimeError(f"embedding is {len(vec)}d, expected 768 — wrong model/host")
    return vec


def _conn():
    pw = os.environ.get("LIFEOS_PGPASSWORD")
    if not pw:
        raise RuntimeError("LIFEOS_PGPASSWORD not set — cannot read the pgvector floor")
    return psycopg2.connect(
        host=os.environ.get("LIFEOS_PGHOST", "localhost"),
        port=os.environ.get("LIFEOS_PGPORT", "5432"),
        user=os.environ.get("LIFEOS_PGUSER", "lifeos"),
        dbname=os.environ.get("LIFEOS_DB", "lifeos"),
        password=pw,
    )


def _ledger_estates(bodies: list) -> dict:
    """sha256 -> (dataset, pointer_only) for anything the LEDGER already knows.

    The ledger is AUTHORITATIVE. §4 made provenance mandatory on every write precisely so
    that a row's estate would be a RECORDED FACT, not a heuristic re-derived at read time.
    Anything written through remember() has a ledger row, so we do not re-guess it — and we
    cannot be wrong about it later if the classifier changes.
    """
    if not bodies:
        return {}
    shas = [hashlib.sha256(b.encode()).hexdigest() for b in bodies if b]
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT sha256, dataset, pointer_only FROM mimir.provenance "
                "WHERE sha256 = ANY(%s)", (shas,))
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    finally:
        conn.close()


def _classify(namespace: str, content: str, source: str, ledger: dict) -> tuple:
    """(dataset, is_confidential). LEDGER FIRST, heuristic only as a fallback."""
    import estates
    import provenance as prov

    known = ledger.get(hashlib.sha256((content or "").encode()).hexdigest())
    if known:
        dataset, pointer_only = known
        return dataset, bool(pointer_only) or dataset == CONFIDENTIAL

    # Legacy rows (written before the one API existed) have no provenance. Fall back to the
    # same classifier the backfill uses -- its tie-break returns WORK, so it fails CLOSED.
    estate, _ambiguous = estates.estate_for_memory(namespace or "", content or "")
    prov_str = f"memory:{namespace or ''}/{source or ''}"
    sensitivity = prov.sensitivity_for(prov_str, estate, content or "")
    if sensitivity == "confidential":
        return CONFIDENTIAL, True
    return estate, False


def recall_floor_sync(query: str, scope: Scope, k: int = 5) -> List[Passage]:
    """Search the incumbent store, but only ever return what this session may see."""
    if not query.strip():
        return []

    vec = _embed(query)
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            # Read-only. No retrieval_count bump, no stats mutation.
            cur.execute(
                "SELECT namespace, source, content "
                "FROM memory.entries "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (str(vec), k * _OVERFETCH),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    ledger = _ledger_estates([r[2] for r in rows])   # one round-trip, not one per row

    out: List[Passage] = []
    seen = set()                      # memory.entries holds ~311k duplicate rows
    for namespace, source, content in rows:
        dataset, confidential = _classify(namespace, content, source, ledger)

        # `shared` is not a thing in the old store; a row is personal or work.
        # If the session cannot see this row's estate, it does not exist for this session.
        if dataset not in scope.datasets:
            continue

        if confidential:
            # The pointer-only guarantee holds HERE TOO. Never return the body.
            title = (content or "").strip().splitlines()[0][:120] if content else ""
            key = ("conf", title)
            if key in seen:
                continue
            seen.add(key)
            out.append(Passage(
                estate=CONFIDENTIAL,
                text=f"[CONFIDENTIAL — pointer only, content withheld] {title}. "
                     f"Retrieve the source directly: memory:{namespace}/{source}",
                origin="floor", pointer_only=True, title=title,
                source=f"memory:{namespace}/{source}",
            ))
        else:
            body = (content or "").strip()
            if not body or body in seen:
                continue
            seen.add(body)
            out.append(Passage(estate=dataset, text=body, origin="floor",
                               source=f"memory:{namespace}"))

        if len(out) >= k:
            break

    return out
