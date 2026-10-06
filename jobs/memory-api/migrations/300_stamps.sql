-- Migration 300 — recency-per-entity STAMPS (spec 08 §2, the keystone).
--
-- Stamps are POINTERS, never copies: (entity, estate, source, last_event_at, ref, event_kind).
-- No bodies, no summaries that can go stale. Sources stay authoritative and are read directly;
-- a stamp answers exactly one question — "when did X last meaningfully change, and where do I
-- look." This is the mig-299 anti-copy ruling extended to retrieval.
--
-- Placement: cognee_prod.mimir, next to mimir.provenance — access is ONLY through the memory
-- API, so the scope kernel applies (a work stamp is invisible to a personal-scoped session;
-- existence itself is signal). Owned by the `cognee` role so the API can manage them.
--
-- Recorded in the db-host migration ledger (ops.schema_migrations) as filename per house rule,
-- even though the tables live in cognee_prod — one migration log.

BEGIN;

CREATE TABLE IF NOT EXISTS mimir.entities (
    entity_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_key     text NOT NULL UNIQUE,          -- 'person:e164:+1555…' | 'person:email:x@y'
                                                  --  | 'project:reeva' | 'bill:electricity'
    kind           text NOT NULL CHECK (kind IN
                     ('person','project','bill','org','device','topic')),
    display_name   text,
    estate_default text NOT NULL CHECK (estate_default IN ('work','personal','shared')),
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Aliases: how a producer's raw identifier maps to an entity. Auto-registration keys the
-- entity by an identifier and adds that identifier as its own alias; canonical naming and
-- merges are later CURATION (repoint aliases) — never an LLM call in the write path.
CREATE TABLE IF NOT EXISTS mimir.entity_aliases (
    alias_norm  text NOT NULL,
    alias_kind  text NOT NULL CHECK (alias_kind IN ('phone','email','name','slug','handle')),
    entity_id   bigint NOT NULL REFERENCES mimir.entities(entity_id) ON DELETE CASCADE,
    UNIQUE (alias_norm, alias_kind)
);
CREATE INDEX IF NOT EXISTS entity_aliases_entity ON mimir.entity_aliases(entity_id);

-- The recency stamp. One row per (entity, estate, source): the newest event and its ref.
-- estate is on the STAMP (m365 -> work, imessage -> personal) so the scope kernel can filter.
CREATE TABLE IF NOT EXISTS mimir.entity_recency (
    entity_id     bigint NOT NULL REFERENCES mimir.entities(entity_id) ON DELETE CASCADE,
    estate        text NOT NULL CHECK (estate IN ('work','personal','shared')),
    source        text NOT NULL,                  -- m365_mail | imessage | ...
    last_event_at timestamptz NOT NULL,
    ref           text NOT NULL,                  -- Graph message id | chat guid | ...
    event_kind    text,                           -- received | unanswered | <action-kind>
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, estate, source)
);
CREATE INDEX IF NOT EXISTS entity_recency_estate ON mimir.entity_recency(estate, last_event_at);

-- Freshness: newest stamp per (entity, estate) + which sources contributed. Read by the
-- pre-flight hook's GET /stamps and (later) recall()'s freshness_hint.
CREATE OR REPLACE VIEW mimir.v_entity_freshness AS
    SELECT r.entity_id,
           e.entity_key,
           e.display_name,
           r.estate,
           max(r.last_event_at)              AS last_event_at,
           array_agg(DISTINCT r.source)      AS sources
    FROM mimir.entity_recency r
    JOIN mimir.entities e USING (entity_id)
    GROUP BY r.entity_id, e.entity_key, e.display_name, r.estate;

-- The API connects as `cognee`; let it own/manage the stamp tables (as it does provenance).
GRANT ALL ON mimir.entities, mimir.entity_aliases, mimir.entity_recency TO cognee;
GRANT SELECT ON mimir.v_entity_freshness TO cognee;

COMMIT;
