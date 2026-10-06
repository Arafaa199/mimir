"""mimir-memory — THE one memory API (spec 07).

Every surface calls this instead of touching a store. Nothing else may read the spine.

    POST /v1/recall    {ingress, query, k, escalate?, datasets?}  -> passages
    GET  /health

THE SHAPE OF THE REQUEST IS THE SECURITY MODEL. Note what a caller CANNOT send:

  * no `estate` / `namespace` it chooses freely — it names its INGRESS, and the registry
    decides what that ingress may see. A caller-chosen namespace is exactly the spoofable
    surface §4 forbids;
  * `datasets` can only NARROW what the ingress already holds (see scope._narrow);
  * `escalate` is refused outright unless the ingress is owner-strong — so a hostile
    Telegram message that sets it buys nothing.

The ingress itself is authenticated by a per-ingress key, NOT by the caller asserting who
it is. `X-Ingress-Key` must match the key registered for that ingress. Without that, the
whole registry is decoration: anyone could claim to be `work_cli`.
"""
import asyncio
import hmac
import os
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# MUST run before anything imports cognee. `nomic_engine` is the PATCHED embedding path
# (bounded concurrency, truncation, a real timeout, fail-loud) — cognee's stock path raises a
# generic EmbeddingException that masks an asyncio.TimeoutError, which is exactly what the
# spec-05 "422" turned out to be. Without install(), every spine recall dies with
# "EmbeddingException: Failed to index data" the moment ollama is under load — and the API
# quietly answers from the floor alone, i.e. the graph we built is never consulted.
import nomic_engine  # noqa: E402  (from ~/mimir-memory, on PYTHONPATH)

nomic_engine.install()

import audit
import stamps as stamps_mod
from ingress import UnknownIngress, lookup
from recall import EstateLeak, recall
from scope import ScopeDenied, mint, strip_escalation_verb
from write import WriteDenied, remember

app = FastAPI(title="mimir-memory", version="0.1")


class RecallRequest(BaseModel):
    ingress: str = Field(..., description="A REGISTERED surface name. Not an estate.")
    query: str
    k: int = Field(5, ge=1, le=50)
    escalate: bool = Field(
        False,
        description="Cross-estate. Set ONLY by deterministic router code that saw the "
                    "owner type the verb — never by a model, never from message content.",
    )
    datasets: Optional[List[str]] = Field(
        None, description="Narrow the scope. Can never widen it."
    )


def _authenticate(ingress: str, presented: Optional[str]) -> None:
    """The ingress must PROVE it is the ingress it claims to be.

    Keys live per-ingress in the environment: MIMIR_KEY_<INGRESS>. An ingress with no key
    configured cannot be used at all — fail closed, rather than defaulting to open.
    """
    expected = os.environ.get(f"MIMIR_KEY_{ingress.upper()}")
    if not expected:
        raise HTTPException(403, f"ingress {ingress!r} has no key configured — refusing")
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(401, "bad or missing X-Ingress-Key")


@app.get("/health")
async def health():
    return {"ok": True, "service": "mimir-memory"}


def _refuse_producer_reads(ingress: str) -> None:
    """A producer is write-stamps-only (invariant §5.2). It must never read the brain — not
    via recall, not via remember. Empty scope already makes recall return nothing, but this is
    the EXPLICIT fail-closed guard so a producer key can never read even if scope logic drifts."""
    if lookup(ingress).is_producer:
        raise HTTPException(403, f"ingress {ingress!r} is a write-only stamp producer and "
                                 f"may not read or write memory (invariant §5.2)")


@app.post("/v1/recall")
async def v1_recall(req: RecallRequest, x_ingress_key: Optional[str] = Header(None)):
    _authenticate(req.ingress, x_ingress_key)
    try:
        _refuse_producer_reads(req.ingress)
    except UnknownIngress as e:
        raise HTTPException(403, str(e)) from None

    try:
        scope = mint(req.ingress, escalate=req.escalate, requested_datasets=req.datasets)
    except (UnknownIngress, ScopeDenied) as e:
        # A DENIAL is the most important row in the audit table. Log it before raising --
        # otherwise the log shows only successful reads, and every blocked injection
        # attempt is invisible, which quietly implies nothing hostile ever arrived.
        audit.record_denial(req.ingress, req.query, f"{type(e).__name__}: {e}")
        raise HTTPException(403, str(e)) from None

    # Strip the escalation verb before the text reaches an embedder or a model.
    query = strip_escalation_verb(req.query)

    try:
        passages, health = await recall(query, scope, k=req.k)
    except EstateLeak as e:
        audit.record_recall(scope, query, 0, error=f"ESTATE_LEAK: {e}")
        # Fail CLOSED and loudly. Never degrade to "return what we have".
        raise HTTPException(500, "estate isolation failure — recall refused") from None
    except Exception as e:  # noqa: BLE001
        audit.record_recall(scope, query, 0, error=f"{type(e).__name__}: {e}")
        raise HTTPException(500, f"recall failed: {type(e).__name__}") from None

    audit.record_recall(scope, query, len(passages))

    # Ask #4 (spec 08): freshness hints for entities the query names. Fail-open ON
    # PURPOSE — recency decoration must never take recall down with it.
    try:
        freshness = stamps_mod.freshness_for_query(query, scope.datasets)
    except Exception as e:  # noqa: BLE001
        print(f"freshness hint failed (recall served without): {type(e).__name__}: {e}", flush=True)
        freshness = []

    return {
        "freshness": freshness,
        # `health` is not decoration. The spine holds an exclusive kuzu lock on whatever
        # estate is being cognified, so an answer may legitimately come from the FLOOR
        # alone. The caller must be able to tell "no memory of this" from "the spine was
        # busy" -- a brain that silently answers from half its memory is worse than one
        # that says which half it used.
        "health": health,
        "scope": {
            "ingress": scope.ingress,
            "datasets": sorted(scope.datasets),
            "escalated": scope.escalated,
            "sink": scope.sink,
            "cross_estate": scope.is_cross_estate,
        },
        # The caller is told where the answer may go. Cross-estate context is owner-direct
        # only; a propose_only sink must never auto-send (design law 1).
        "passages": [
            {
                "estate": p.estate,
                "text": p.text,
                "origin": p.origin,
                "pointer_only": p.pointer_only,
                "title": p.title,
                "source": p.source,
            }
            for p in passages
        ],
        "count": len(passages),
    }


class RememberRequest(BaseModel):
    ingress: str = Field(..., description="A REGISTERED surface. Untrusted ones cannot write.")
    text: str
    title: str = ""
    source: str = ""
    tags: Optional[List[str]] = None
    datasets: Optional[List[str]] = Field(
        None, description="Narrow the session so the write has exactly ONE target estate."
    )
    declassify: bool = Field(
        False,
        description="Write to `shared` — visible from BOTH estates. This is "
                    "declassification: owner + strong auth only, never implicit.",
    )


@app.post("/v1/remember")
async def v1_remember(req: RememberRequest, x_ingress_key: Optional[str] = Header(None)):
    """Write one memory.

    A bad read is an incident; a bad WRITE is a belief — every future recall on every
    surface will repeat it faithfully. Hence: untrusted principals cannot write at all,
    the estate comes from the scope, cross-estate sessions do not write back, `shared` is
    explicit declassification, and anything that classifies confidential is kept
    pointer-only (its body never reaches an LLM).
    """
    _authenticate(req.ingress, x_ingress_key)
    try:
        _refuse_producer_reads(req.ingress)
    except UnknownIngress as e:
        raise HTTPException(403, str(e)) from None

    try:
        scope = mint(req.ingress, requested_datasets=req.datasets)
    except (UnknownIngress, ScopeDenied) as e:
        audit.record_denial(req.ingress, req.text, f"{type(e).__name__}: {e}")
        raise HTTPException(403, str(e)) from None

    try:
        result = await asyncio.to_thread(
            remember, req.text, scope,
            title=req.title, source=req.source, tags=req.tags, declassify=req.declassify,
        )
    except WriteDenied as e:
        audit.record_denial(req.ingress, req.text, f"WriteDenied: {e}")
        raise HTTPException(403, str(e)) from None
    except Exception as e:  # noqa: BLE001
        audit.record_recall(scope, req.text, 0, error=f"write {type(e).__name__}: {e}")
        raise HTTPException(500, f"write failed: {type(e).__name__}") from None

    audit.record_write(scope, req.text, result)

    return {
        "sha256": result.sha256,
        "dataset": result.dataset,
        "sensitivity": result.sensitivity,
        # `recallable` is the honest answer to "can I find this again?" -- true the instant
        # the write lands in the pgvector floor. `cognified` says whether the GRAPH has it
        # yet, which takes ~140s of LLM extraction and happens in the drain.
        "recallable": result.recallable,
        "cognified": result.cognified,
        "note": result.note,
    }


# ============================================================================
# Stamps (spec 08 §6) — recency hints. Write = producers only; read = scope-filtered.
# ============================================================================

class StampItem(BaseModel):
    entity_key: str = Field(..., description="'person:e164:+1555…' | 'project:reeva' | …")
    estate: str = Field(..., description="Pinned to the producer; must be in its stamps_write.")
    source: str
    last_event_at: str = Field(..., description="ISO8601.")
    ref: str = Field(..., description="Pointer to the authoritative source (Graph id, guid).")
    event_kind: Optional[str] = None
    kind: str = "topic"
    display_name: Optional[str] = None
    alias: Optional[str] = None
    alias_kind: Optional[str] = None


class StampsWriteRequest(BaseModel):
    ingress: str
    stamps: List[StampItem]


@app.post("/v1/stamps")
async def v1_stamps_write(req: StampsWriteRequest, x_ingress_key: Optional[str] = Header(None)):
    """Batch-upsert stamps. ONLY a producer ingress may write, and ONLY to its pinned estate.

    A reader ingress (cli, claude_code, …) is refused here: writing recency hints is a
    producer capability, and conflating it with a reader would let a leaked reader key forge
    hints. The estate is checked per stamp against the ingress's `stamps_write` — one
    out-of-estate stamp fails the whole batch (see stamps.write_stamps)."""
    _authenticate(req.ingress, x_ingress_key)
    ing = lookup(req.ingress)
    if not ing.stamps_write:
        audit.record_stamp_write(req.ingress, len(req.stamps), 0,
                                 denied="ingress is not a stamp producer")
        raise HTTPException(403, f"ingress {req.ingress!r} may not write stamps "
                                 f"(not a producer)")
    items = [stamps_mod.Stamp(**s.model_dump()) for s in req.stamps]
    try:
        r = await asyncio.to_thread(
            stamps_mod.write_stamps, items, ing.stamps_write, req.ingress)
    except stamps_mod.StampDenied as e:
        audit.record_stamp_write(req.ingress, len(items), 0, denied=str(e))
        raise HTTPException(403, str(e)) from None
    except Exception as e:  # noqa: BLE001
        audit.record_stamp_write(req.ingress, len(items), 0, denied=f"{type(e).__name__}")
        raise HTTPException(500, f"stamp write failed: {type(e).__name__}") from None
    audit.record_stamp_write(req.ingress, r["written"], r["entities_created"])
    # Dead-man (spec 08 #6): a successful write marks stamps_<source> fresh, so a silently-dead
    # producer becomes indistinguishable-from-a-quiet-mailbox no longer — the watchdog alarms.
    audit.stamp_freshness({s.source for s in items})
    return r


@app.get("/v1/stamps")
async def v1_stamps_read(entities: str, ingress: str = "claude_code",
                         x_ingress_key: Optional[str] = Header(None)):
    """Freshness for `entities` (comma-separated entity_keys), FILTERED to the reader's scope.
    A work stamp is invisible to a personal-scoped ingress — existence is signal, not just
    content. A producer (empty scope) gets nothing: it may write but never read."""
    _authenticate(ingress, x_ingress_key)
    try:
        scope = mint(ingress)
    except (UnknownIngress, ScopeDenied) as e:
        raise HTTPException(403, str(e)) from None
    keys = [e.strip() for e in entities.split(",") if e.strip()]
    rows = await asyncio.to_thread(stamps_mod.read_stamps, keys, scope.datasets)
    audit.record_stamp_read(ingress, scope.datasets, keys, len(rows), "stamps_read")
    return {"scope": sorted(scope.datasets), "stamps": rows, "count": len(rows)}


@app.get("/v1/aliases")
async def v1_aliases_export(ingress: str = "claude_code",
                            x_ingress_key: Optional[str] = Header(None)):
    """Alias→entity map for the pre-flight hook's local cache, scope-filtered. Only aliases of
    entities that have a stamp visible to this scope are exported (existence is signal)."""
    _authenticate(ingress, x_ingress_key)
    try:
        scope = mint(ingress)
    except (UnknownIngress, ScopeDenied) as e:
        raise HTTPException(403, str(e)) from None
    aliases = await asyncio.to_thread(stamps_mod.export_aliases, scope.datasets)
    audit.record_stamp_read(ingress, scope.datasets, None, len(aliases), "alias_export")
    return {"scope": sorted(scope.datasets), "aliases": aliases, "count": len(aliases)}
