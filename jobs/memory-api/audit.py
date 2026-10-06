"""Audit every recall to `ops.action_audit` (spec 07; the table from migration 287).

An estate boundary you cannot PROVE is not a boundary — it is a hope. This is the record
that turns "work never leaked into personal" from an assertion into a query, and that makes
a Work offboarding purge demonstrable rather than merely claimed.

The query text is HASHED, never stored: the audit log must not become a second, unscoped
copy of the very thing it is guarding.

Schema note (verified live, not assumed): the trust engine's table lives in the **`db-host`**
database (not `lifeos`) and its columns are
(agent, surface, domain, action, params jsonb, decision, trust_pct_at, tier_at, approver,
idempotency_key, result jsonb), with decision ∈ {proposed, approved, denied, auto_executed,
executed, failed}.
"""
import hashlib
import json
import os
from typing import Optional

import psycopg2

_DSN = {
    "host": os.environ.get("LIFEOS_PGHOST", "localhost"),
    "port": os.environ.get("LIFEOS_PGPORT", "5432"),
    "user": os.environ.get("LIFEOS_PGUSER", "lifeos"),
    "dbname": os.environ.get("LIFEOS_DB", "lifeos"),
}


def _write(surface: str, decision: str, tier: str, params: dict, result: dict,
           action: str = "recall") -> None:
    pw = os.environ.get("LIFEOS_PGPASSWORD")
    if not pw:
        print("AUDIT: LIFEOS_PGPASSWORD unset — served UNAUDITED", flush=True)
        return
    try:
        conn = psycopg2.connect(password=pw, **_DSN)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ops.action_audit "
                    "(agent, surface, domain, action, params, decision, tier_at, result) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    ("mimir-memory", surface, "memory_recall", action,
                     json.dumps(params), decision, tier, json.dumps(result)),
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"AUDIT WRITE FAILED: {type(exc).__name__}: {exc}", flush=True)


def record_denial(ingress: str, query: str, reason: str) -> None:
    """A REFUSED request — the most important row in this table.

    Denials happen before a Scope exists (mint() raised), so they cannot go through
    record_recall. Without this, the audit log would show only the requests that
    SUCCEEDED — every blocked injection attempt would be invisible, and the log would
    quietly imply nothing hostile ever arrived.
    """
    _write(
        surface=ingress,
        decision="denied",
        tier="propose",
        params={
            "ingress": ingress,
            "denied_reason": reason,
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "query_chars": len(query),
        },
        result={"n_results": 0},
    )


def record_recall(scope, query: str, n_results: int, *, error: Optional[str] = None) -> None:
    """One row per recall. Never fails the caller's read.

    A broken audit writer must not become a denial-of-service lever on the whole brain, so
    a write failure is logged loudly and the recall still serves. It is never silent.
    """
    params = {
        "ingress": scope.ingress,
        "principal": scope.principal,
        "datasets": sorted(scope.datasets),
        "escalated": scope.escalated,
        "cross_estate": scope.is_cross_estate,
        "sink": scope.sink,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "query_chars": len(query),
    }
    _write(
        surface=scope.ingress,
        decision="failed" if error else "executed",
        tier="propose" if scope.sink == "propose_only" else "bounded",
        params=params,
        result={"n_results": n_results, "error": error},
    )


def record_write(scope, text: str, result) -> None:
    """A write is the most consequential thing this API does — it creates a BELIEF.
    Audit it with the same rigour as a recall, plus what was actually decided."""
    _write(
        surface=scope.ingress,
        decision="executed",
        tier="bounded",
        params={
            "ingress": scope.ingress,
            "principal": scope.principal,
            "dataset": result.dataset,
            "sensitivity": result.sensitivity,
            "sha256": result.sha256,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text_chars": len(text),
        },
        result={"cognified": result.cognified, "recallable": result.recallable,
                "note": result.note},
        action="remember",
    )


def record_stamp_write(ingress: str, n_stamps: int, n_created: int, *, denied: str = None) -> None:
    """Every stamp write, allowed or denied (spec 08 §6.1 — audited like recall).
    A denied write is a producer trying to cross its estate — worth keeping."""
    _write(
        surface=ingress,
        decision="denied" if denied else "executed",
        tier="propose",
        params={"ingress": ingress, "op": "stamp_write", "n_stamps": n_stamps,
                "denied_reason": denied},
        result={"written": 0 if denied else n_stamps, "entities_created": n_created},
        action="stamps_write",
    )


def record_stamp_read(ingress: str, datasets, entity_keys, n_results: int, op: str) -> None:
    """Every stamp/alias read, scope recorded — so a read of another estate's stamp existence
    is provable after the fact (existence is signal)."""
    import hashlib
    _write(
        surface=ingress,
        decision="executed",
        tier="bounded",
        params={"ingress": ingress, "op": op, "scope": sorted(datasets),
                "n_entities_asked": len(entity_keys or []),
                "entities_sha256": hashlib.sha256(
                    ",".join(sorted(entity_keys or [])).encode()).hexdigest()},
        result={"n_results": n_results},
        action="stamps_read",
    )


def stamp_freshness(sources) -> None:
    """Dead-man freshness for stamp producers (spec 08 ask #6). On a SUCCESSFUL POST /v1/stamps,
    mark ops.data_versions domain `stamps_<family>` fresh — because a producer runs on the
    laptop and CANNOT reach db-host-db by design (invariant §5.3), so the server stamps freshness
    on its behalf, and only when a write genuinely lands.

    migs 293-296 law: success-only, never on failure, never a now() DEFAULT. `now()` here is
    honest — unlike a backup (stamped later than it ran), the write succeeded in THIS request,
    so now() is its true time, not a faked freshness. The row is created by the first real
    success (born from a real event), not pre-seeded with a default.

    Fail-soft: if this update fails, the domain simply goes stale and the watchdog alarms —
    the CORRECT outcome. A dead-man that broke the write it guards would be worse than the
    silent death it exists to catch.
    """
    pw = os.environ.get("LIFEOS_PGPASSWORD")
    if not pw:
        print("STAMP FRESHNESS: LIFEOS_PGPASSWORD unset — domain not marked fresh", flush=True)
        return
    # m365_mail -> stamps_m365 ; imessage -> stamps_imessage. One domain per source FAMILY,
    # matching spec §2's stamps_m365 / stamps_imessage.
    domains = sorted({"stamps_" + str(s).split("_", 1)[0] for s in sources if s})
    if not domains:
        return
    try:
        conn = psycopg2.connect(password=pw, **_DSN)
        try:
            with conn, conn.cursor() as cur:
                for d in domains:
                    cur.execute(
                        "INSERT INTO ops.data_versions (domain, last_modified_at) "
                        "VALUES (%s, now()) "
                        "ON CONFLICT (domain) DO UPDATE SET last_modified_at = now()",
                        (d,),
                    )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"STAMP FRESHNESS update failed (stamp still written): "
              f"{type(exc).__name__}: {exc}", flush=True)
