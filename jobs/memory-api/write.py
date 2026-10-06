"""remember() — the write path (spec 07 / spec 05 §4).

Writing is more dangerous than reading. A bad read shows the owner something they should not
have seen, once. A bad write puts it in the brain FOREVER, where every future recall on every
surface repeats it faithfully. Prompt injection that reaches memory stops being an incident
and becomes a belief. Hence rules stricter than recall's:

1. **Untrusted principals cannot write. At all.** Not "propose and we'll see" — a Telegram
   group member or an inbound email puts NOTHING in memory. Design law 5 applied to the most
   durable action there is. If the owner wants something from a group chat remembered, they
   say it themselves, from their own surface.
2. **The estate comes from the SCOPE, never the caller.** Same rule as recall.
3. **A cross-estate session does not write back** (§4 v1): it holds both estates, so there is
   no safe default, and guessing means personal facts landing in work.
4. **`shared` is DECLASSIFICATION** — owner + strong auth + an explicit flag. Never implicit.
5. **A write that classifies CONFIDENTIAL is kept pointer-only**: body retained in our own
   table, never embedded, never cognified, never sent to any LLM. The write path must not
   become the hole the backfill refused to be.

## Why writes are FAST and cognification is LATER

Cognify costs ~140 s/doc (an LLM reads the text and extracts a graph). A write that blocks on
that is a write that times out — the first live test did exactly that. So:

    remember()  ->  durable row + embedding + pgvector  (~200 ms, immediately recallable)
    drain       ->  cognify into the graph              (later, batched, retryable)

This is what "pgvector is the recall FLOOR" buys us: a new memory is answerable the instant it
is written, and the graph enriches it afterwards. It also survives kuzu holding an exclusive
lock on an estate being backfilled — durability never depends on the graph being free.
"""
import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional

import psycopg2

from ingress import CONFIDENTIAL, PERSONAL, SHARED, lookup
from scope import Scope

# Everything written through this API carries this namespace in the old store. It is NOT how
# the estate is decided -- the LEDGER is (see floor.py). It is a label, not a claim.
API_NAMESPACE = "mimir"


class WriteDenied(Exception):
    """The session may not write what it is asking to write. Always audited."""


@dataclass(frozen=True)
class WriteResult:
    sha256: str
    dataset: str
    sensitivity: str
    recallable: bool      # answerable NOW (via the pgvector floor)
    cognified: bool       # in the graph yet?
    note: str


def _spine_conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


def _store_conn():
    return psycopg2.connect(
        host=os.environ.get("LIFEOS_PGHOST", "localhost"),
        port=os.environ.get("LIFEOS_PGPORT", "5432"),
        user=os.environ.get("LIFEOS_PGUSER", "lifeos"),
        dbname=os.environ.get("LIFEOS_DB", "lifeos"),
        password=os.environ["LIFEOS_PGPASSWORD"],
    )


def _target_dataset(scope: Scope, declassify: bool) -> str:
    """Which single estate does this write belong to? Refuse to guess."""
    ing = lookup(scope.ingress)

    if scope.principal != "owner":
        raise WriteDenied(
            f"ingress {scope.ingress!r} has principal={scope.principal!r} and may not write "
            f"to memory. Untrusted input becomes a permanent belief if written — the owner "
            f"must say it themselves, from their own surface."
        )
    if declassify:
        if ing.auth != "strong":
            raise WriteDenied(
                "writing to `shared` is DECLASSIFICATION — it makes a fact visible from "
                "both estates — and needs a strong-auth owner surface."
            )
        return SHARED
    if scope.escalated:
        raise WriteDenied(
            "a cross-estate session does not write back (§4, v1). It can see both estates, "
            "so there is no safe default for where a new memory belongs. Write it from a "
            "single-estate session, or pass declassify=True to put it in `shared`."
        )

    # `shared` is never an implicit target; `work_confidential` is ROUTED to, never chosen.
    candidates = sorted(scope.datasets - {SHARED, CONFIDENTIAL})
    if len(candidates) != 1:
        raise WriteDenied(
            f"this session holds {candidates or ['nothing writable']} — a write needs exactly "
            f"one estate. Narrow the session (datasets=[...]) and try again."
        )
    return candidates[0]


def remember(
    text: str,
    scope: Scope,
    *,
    title: str = "",
    source: str = "",
    tags: Optional[List[str]] = None,
    declassify: bool = False,
) -> WriteResult:
    import provenance as prov          # the SAME classifier the backfill uses

    body = (text or "").strip()
    if not body:
        raise WriteDenied("refusing to write an empty memory")

    dataset = _target_dataset(scope, declassify)
    estate = PERSONAL if dataset == SHARED else dataset

    # source_trust is DERIVED from the authenticated ingress, never taken from the caller --
    # a caller that could assert its own trust could launder an injected claim into an
    # `owner`-trusted memory.
    ing = lookup(scope.ingress)
    # Vocabulary is the provenance CHECK's: owner/agent/external/untrusted. Non-strong
    # owner surfaces map to "agent" — honest for the iOS path, whose saves default to
    # agent_observation (the app's Gemini deciding), not the owner's own words. "device"
    # was never in the CHECK; it CheckViolation'd the first time a non-strong ingress
    # wrote (ios, 2026-07-20 — every earlier writer was strong-auth).
    source_trust = "owner" if ing.auth == "strong" else "agent"
    src = source or f"api:{scope.ingress}"
    prov_str = f"api:{scope.ingress}/{src}"

    sensitivity = prov.sensitivity_for(prov_str, estate, body)
    if sensitivity == "confidential":
        dataset = CONFIDENTIAL          # route, don't refuse: the memory is KEPT, pointer-only

    sha = hashlib.sha256(body.encode()).hexdigest()
    head = (title or body.splitlines()[0])[:400]

    # 1. DURABLE FIRST -- before any embedding, any graph, anything that can be busy.
    conn = _spine_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mimir.pending_writes (sha256, title, body, dataset, source, "
                " source_trust, estate, sensitivity, ingress, tags) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (sha256) DO NOTHING",
                (sha, head, body, dataset, src, source_trust, estate, sensitivity,
                 scope.ingress, tags or []),
            )
    finally:
        conn.close()

    _ledger(sha, head, src, source_trust, estate, sensitivity, dataset,
            pointer_only=(dataset == CONFIDENTIAL))

    # 2. Confidential: stop here. Not embedded, not cognified, not copied into the shared
    #    store. Its body stays in one access-controlled table and recall yields a pointer.
    if dataset == CONFIDENTIAL:
        _mark_done(sha)
        return WriteResult(sha, dataset, sensitivity, recallable=True, cognified=False,
                           note="registered pointer-only — body never embedded, never sent "
                                "to any LLM")

    # 3. Make it RECALLABLE NOW via the pgvector floor (~200ms), rather than making the owner
    #    wait ~140s for graph extraction. The graph catches up in the drain.
    _insert_floor(sha, head, body, estate, tags or [])

    return WriteResult(sha, dataset, sensitivity, recallable=True, cognified=False,
                       note="stored and immediately recallable (pgvector floor); the graph "
                            "is enriched by the drain")


def _insert_floor(sha: str, title: str, body: str, estate: str, tags: List[str]) -> None:
    """Insert into `memory.entries` with the intake's hardened dedup pattern.

    memory.entries has NO general content-unique index (that is how it accumulated ~311k
    duplicate rows). The intake's fix: cheap check, embed OUTSIDE any transaction (holding a
    pooled connection across a multi-second network call starves the dashboard), then
    re-check under an advisory lock before inserting.
    """
    from floor import _embed

    created_by = "mimir-memory"
    conn = _store_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM memory.entries WHERE created_by=%s AND content=%s "
                        "LIMIT 1", (created_by, body))
            if cur.fetchone():
                return
    finally:
        conn.close()

    vec = _embed(body)                      # outside any transaction, deliberately

    conn = _store_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (created_by + body,))
            cur.execute("SELECT 1 FROM memory.entries WHERE created_by=%s AND content=%s "
                        "LIMIT 1", (created_by, body))
            if cur.fetchone():
                return
            # `category` and `source` are CHECK-constrained enums on this table:
            #   category in (profile, state, archive)
            #   source   in (message, tool_result, agent_observation, user_correction,
            #                migration, doc_seed, synthesis, horus)
            # `user_correction` is the honest source for an owner-stated fact -- and it is
            # exactly what provenance.source_trust_for() maps to `owner`, so the old store
            # and the ledger agree about trust instead of quietly disagreeing.
            cur.execute(
                "INSERT INTO memory.entries (category, content, source, created_by, "
                " namespace, visibility, tags, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                ("state", body, "user_correction", created_by, API_NAMESPACE, "private",
                 tags, str(vec)),
            )
        conn.commit()
    finally:
        conn.close()


def _ledger(sha: str, title: str, source: str, source_trust: str, estate: str,
            sensitivity: str, dataset: str, *, pointer_only: bool) -> None:
    """The ledger is AUTHORITATIVE for anything written through this API (floor.py trusts it
    over the heuristic classifier). That is what §4's mandatory provenance is FOR."""
    conn = _spine_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mimir.provenance (sha256, source, source_trust, estate, "
                " sensitivity, dataset, title, pointer_only, stage) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'live') "
                "ON CONFLICT (sha256) DO UPDATE SET cognified_at = now()",
                (sha, source, source_trust, estate, sensitivity, dataset, title[:400],
                 pointer_only),
            )
    finally:
        conn.close()


def _mark_done(sha: str) -> None:
    conn = _spine_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE mimir.pending_writes SET cognified_at = now() "
                        "WHERE sha256 = %s", (sha,))
    finally:
        conn.close()
