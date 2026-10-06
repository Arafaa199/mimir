"""Recency stamps — write (producers) and read (scope-filtered) (spec 08 §2/§6).

A stamp is a POINTER: (entity, estate, source, last_event_at, ref, event_kind). No body, no
summary. It answers one question — when did X last change, and where do I look — so the read
path (pre-flight hook) knows *when* to fetch from the authoritative source, never storing a
copy that can rot (the mig-299 anti-copy ruling, extended to retrieval).

Two rules carry the security weight:

1. **Estate is pinned at write.** A producer may only stamp the estate(s) in its
   `stamps_write` (m365→work, imessage→personal). It is never taken from the request body —
   same discipline as recall scope: the transport decides, not the content.

2. **Reads are scope-filtered, and EXISTENCE is signal.** `GET /stamps` returns only stamps
   whose estate is in the reader's recall scope. A work stamp is not redacted for a
   personal-scoped session — it is INVISIBLE. Learning that a work entity exists (or changed)
   is itself a cross-estate leak, so a personal ingress must not even see the row.

Auto-registration is deterministic: an unknown identifier-keyed entity is created on first
stamp, with the identifier as its own alias. No LLM in the write path (design law 2 / §2).
"""
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras


# --- Fix A: display_name -> guarded `name` alias -----------------------------------------
# A person's display_name unlocks PERSON routing (65+ named person entities, zero `name`
# aliases, so the alias-only pre-flight hook can never fire on a person's name). But a
# contact literally named "Home"/"Today"/"Mom" would then inject on EVERY prompt. So a name
# becomes a matchable alias ONLY if it is specific enough to not collide with everyday prompt
# language. Err toward NOT registering: a missed person-alias is invisible; a false one is
# noise on every prompt. The guard is the only real design call here.
_MIN_NAME_LEN = 5  # single-token names shorter than this are rejected ("Mum", "Ojas").

# Common words / relationship + place + time labels / functional mailbox terms / given-names
# that double as everyday words. Lowercased, whitespace-collapsed. Conservative but real:
# every entry is something that plausibly appears in an ordinary prompt.
_NAME_STOPLIST = frozenset({
    # relationship / people labels
    "me", "mum", "mom", "mother", "mama", "mommy", "dad", "papa", "daddy", "dada",
    "wife", "husband", "hubby", "partner", "son", "daughter", "kids", "child",
    "brother", "sister", "bro", "sis", "sibling", "family", "grandma", "grandpa",
    "granny", "grandad", "grandmother", "grandfather", "uncle", "aunt", "auntie",
    "cousin", "boss", "manager", "friend", "buddy", "mate", "baby", "love", "neighbour",
    # places / time / routine that show up constantly in prompts
    "home", "house", "office", "school", "college", "uni", "work", "gym", "dentist",
    "doctor", "hospital", "clinic", "pharmacy", "bank", "airport", "hotel", "shop",
    "store", "today", "tomorrow", "yesterday", "tonight", "morning", "afternoon",
    "evening", "night", "lunch", "dinner", "breakfast", "meeting", "weekend", "holiday",
    # generic / functional mailboxes (seen in the real data + common)
    "admin", "support", "birthdays", "info", "help", "helpdesk", "sales", "hr",
    "payroll", "finance", "accounts", "accounting", "billing", "noreply", "no-reply",
    "notifications", "team", "reception", "reports", "webmaster", "postmaster",
    "human resources",
    # single given-names that collide with everyday prompt words
    "grace", "faith", "hope", "summer", "april", "may", "june", "will", "joy", "rose",
    "newsletter",
})


def _norm_name(display_name: str) -> str:
    """Lowercase + whitespace-collapse — the same normalization the hook applies to prompts,
    so a stored `name` alias matches what `re.search(r"\\b<alias>\\b", prompt.lower())` sees."""
    return re.sub(r"\s+", " ", display_name.strip().lower())


def name_alias_ok(display_name: Optional[str]) -> Optional[str]:
    """Return the normalized `name` alias to register for `display_name`, or None to skip it.

    The false-positive guard (spec 08 Fix A): register a name only when it is specific enough
    that it won't fire on ordinary prompt words:
      * not empty and not an email masquerading as a name (contains '@');
      * multi-token OR length >= _MIN_NAME_LEN (rejects short single tokens like "Mum");
      * not in _NAME_STOPLIST (rejects "admin", "today", "human resources", "grace", ...).
    Pure + deterministic (no LLM, no network) — used by BOTH write_stamps and the backfill,
    so the two share one guard policy.
    """
    if not display_name:
        return None
    norm = _norm_name(display_name)
    if not norm or "@" in norm:              # empty, or an email address, not a real name
        return None
    if " " not in norm and len(norm) < _MIN_NAME_LEN:  # short single token -> too collidey
        return None
    if norm in _NAME_STOPLIST:               # common word / label / collidey given-name
        return None
    return norm


class StampDenied(Exception):
    """A producer tried to stamp an estate it does not hold. Fail closed, audited."""


@dataclass(frozen=True)
class Stamp:
    entity_key: str
    estate: str
    source: str
    last_event_at: str            # ISO8601
    ref: str
    event_kind: Optional[str] = None
    kind: str = "topic"           # entity kind, for auto-registration
    display_name: Optional[str] = None
    alias: Optional[str] = None       # the raw identifier to register as an alias
    alias_kind: Optional[str] = None  # phone|email|name|slug|handle


def _conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


def write_stamps(stamps: List[Stamp], allowed_estates: frozenset, ingress: str) -> dict:
    """Batch upsert. Every stamp's estate MUST be in `allowed_estates` (the ingress's
    stamps_write) — one out-of-estate stamp fails the WHOLE batch closed, so a producer can
    never smuggle a cross-estate hint by mixing it into a legitimate batch."""
    if not stamps:
        return {"written": 0, "entities_created": 0}

    for s in stamps:
        if s.estate not in allowed_estates:
            raise StampDenied(
                f"ingress {ingress!r} may stamp {sorted(allowed_estates)} but a stamp targets "
                f"{s.estate!r} (entity {s.entity_key!r}). Estate is pinned to the producer — "
                f"the whole batch is refused."
            )

    conn = _conn()
    created = 0
    try:
        with conn, conn.cursor() as cur:
            for s in stamps:
                # 1. auto-register the entity (deterministic upsert on entity_key).
                cur.execute(
                    "INSERT INTO mimir.entities (entity_key, kind, display_name, "
                    " estate_default, created_by) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (entity_key) DO UPDATE SET "
                    "  display_name = COALESCE(mimir.entities.display_name, EXCLUDED.display_name) "
                    "RETURNING entity_id, (xmax = 0) AS inserted, display_name",
                    (s.entity_key, s.kind, s.display_name, s.estate, ingress),
                )
                entity_id, inserted, eff_display_name = cur.fetchone()
                if inserted:
                    created += 1
                # 2. register the raw identifier as an alias (idempotent).
                if s.alias and s.alias_kind:
                    cur.execute(
                        "INSERT INTO mimir.entity_aliases (alias_norm, alias_kind, entity_id) "
                        "VALUES (%s,%s,%s) ON CONFLICT (alias_norm, alias_kind) DO NOTHING",
                        (s.alias, s.alias_kind, entity_id),
                    )
                # 2b. Fix A: register the EFFECTIVE display_name as a guarded `name` alias
                # (idempotent). Using the post-upsert display_name means this one code path
                # also BACKFILLS existing named entities: the next producer run that re-stamps
                # a person who has a display_name but no name-alias adds it — once it clears the
                # false-positive guard. name_alias_ok() returns None for anything too collidey.
                name_alias = name_alias_ok(eff_display_name)
                if name_alias:
                    cur.execute(
                        "INSERT INTO mimir.entity_aliases (alias_norm, alias_kind, entity_id) "
                        "VALUES (%s,'name',%s) ON CONFLICT (alias_norm, alias_kind) DO NOTHING",
                        (name_alias, entity_id),
                    )
                # 3. the stamp itself — newest-wins per (entity, estate, source).
                cur.execute(
                    "INSERT INTO mimir.entity_recency (entity_id, estate, source, "
                    " last_event_at, ref, event_kind) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (entity_id, estate, source) DO UPDATE SET "
                    "  last_event_at = GREATEST(mimir.entity_recency.last_event_at, "
                    "                           EXCLUDED.last_event_at), "
                    "  ref = CASE WHEN EXCLUDED.last_event_at >= "
                    "               mimir.entity_recency.last_event_at "
                    "             THEN EXCLUDED.ref ELSE mimir.entity_recency.ref END, "
                    "  event_kind = CASE WHEN EXCLUDED.last_event_at >= "
                    "                      mimir.entity_recency.last_event_at "
                    "                    THEN EXCLUDED.event_kind "
                    "                    ELSE mimir.entity_recency.event_kind END, "
                    "  updated_at = now()",
                    (entity_id, s.estate, s.source, s.last_event_at, s.ref, s.event_kind),
                )
    finally:
        conn.close()
    return {"written": len(stamps), "entities_created": created}


def read_stamps(entity_keys: List[str], scope_estates: frozenset) -> List[dict]:
    """Freshness for named entities, filtered to the reader's scope. Estates outside scope are
    INVISIBLE (existence is signal), not redacted — so a personal session gets no hint that a
    work entity even exists."""
    if not entity_keys or not scope_estates:
        return []
    conn = _conn()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT entity_key, display_name, estate, last_event_at, sources, event_kind "
                "FROM mimir.v_entity_freshness "
                "WHERE entity_key = ANY(%s) AND estate = ANY(%s) "
                "ORDER BY last_event_at DESC",
                (entity_keys, list(scope_estates)),
            )
            return [
                {"entity": r["entity_key"], "display_name": r["display_name"],
                 "estate": r["estate"],
                 "last_event_at": r["last_event_at"].isoformat() if r["last_event_at"] else None,
                 "sources": r["sources"], "event_kind": r["event_kind"]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def freshness_for_query(query: str, scope_estates: frozenset, limit: int = 5):
    """Ask #4 (spec 08): recency hints for entities MENTIONED in the query, scope-filtered.

    Deterministic — alias substring match against the query text, no LLM, same
    invisibility rule as read_stamps (an out-of-scope entity's existence is signal).
    The CALLER must treat this as fail-open decoration: a freshness bug must never
    break recall."""
    if not query or not scope_estates:
        return []
    q = query.lower()
    conn = _conn()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT f.entity_key, f.display_name, f.estate, f.last_event_at, "
                "f.sources, f.event_kind "
                "FROM mimir.entity_aliases a "
                "JOIN mimir.v_entity_freshness f USING (entity_id) "
                "WHERE f.estate = ANY(%s) AND length(a.alias_norm) >= 3 "
                "AND position(a.alias_norm in %s) > 0 "
                "ORDER BY f.last_event_at DESC NULLS LAST LIMIT %s",
                (list(scope_estates), q, limit),
            )
            return [
                {"entity": r["entity_key"], "display_name": r["display_name"],
                 "estate": r["estate"],
                 "last_event_at": r["last_event_at"].isoformat() if r["last_event_at"] else None,
                 "sources": r["sources"], "event_kind": r["event_kind"]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def export_aliases(scope_estates: frozenset) -> List[dict]:
    """Alias -> entity_key map for the hook's LOCAL cache, scope-filtered by the entity's
    stamp estates. An alias whose entity has no stamp visible to this scope is not exported
    (existence is signal, again). Returns only what a string-match needs: no timestamps, no
    refs — those come from GET /stamps on a match."""
    if not scope_estates:
        return []
    conn = _conn()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT a.alias_norm, a.alias_kind, e.entity_key "
                "FROM mimir.entity_aliases a "
                "JOIN mimir.entities e USING (entity_id) "
                "WHERE EXISTS (SELECT 1 FROM mimir.entity_recency r "
                "              WHERE r.entity_id = e.entity_id AND r.estate = ANY(%s))",
                (list(scope_estates),),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def backfill_name_aliases(dry_run: bool = False) -> dict:
    """One-shot backfill (Fix A): register a guarded `name` alias for every existing entity
    that already has a display_name. Reuses name_alias_ok() — the SAME guard the live write
    path uses — so the backfill and future producer runs agree on what is safe to register.
    Idempotent (ON CONFLICT DO NOTHING); safe to re-run and safe to dry-run first.

    Returns counts + examples for the acceptance report (registered vs skipped, and WHY)."""
    conn = _conn()
    registered = inserted = skipped = 0
    registered_names: List[str] = []
    skipped_names: List[str] = []
    rows: List[dict] = []
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT entity_id, display_name FROM mimir.entities "
                "WHERE display_name IS NOT NULL ORDER BY entity_id"
            )
            rows = cur.fetchall()
            for r in rows:
                alias = name_alias_ok(r["display_name"])
                if not alias:
                    skipped += 1
                    skipped_names.append(r["display_name"])
                    continue
                registered += 1
                registered_names.append(alias)
                if not dry_run:
                    cur.execute(
                        "INSERT INTO mimir.entity_aliases (alias_norm, alias_kind, entity_id) "
                        "VALUES (%s,'name',%s) ON CONFLICT (alias_norm, alias_kind) DO NOTHING",
                        (alias, r["entity_id"]),
                    )
                    inserted += cur.rowcount
    finally:
        conn.close()
    return {"candidates": len(rows), "guard_passed": registered, "inserted": inserted,
            "skipped": skipped, "registered_names": registered_names,
            "skipped_names": skipped_names, "dry_run": dry_run}
