-- Migration 315 — surface event_kind through v_entity_freshness (spec 08 Fix B).
-- (314 was taken by LifeOS 314_financial_position_include_cash — filename-keyed ledger, but
--  renumbered to 315 to keep the shared sequence unambiguous.)
--
-- read_stamps / freshness_for_query read mimir.v_entity_freshness, but the view dropped
-- event_kind — so the single most ACTIONABLE signal (unanswered / reply_needed sitting in
-- entity_recency) never reached a prompt. Spec 08 §3 step 3 specified "(<event_kind>)" on the
-- injected pre-flight line; the impl dropped it. This restores it.
--
-- The view groups by (entity, estate) over potentially many sources, so event_kind is the
-- kind of the MOST RECENT stamp for that (entity, estate) — the same row whose last_event_at
-- the line already reports ("last update <ts> ... (<event_kind>)"). array_agg(... ORDER BY
-- last_event_at DESC)[1] picks it deterministically within the existing GROUP BY.
--
-- Placement: cognee_prod.mimir (same DB as mig 300). Recorded in the db-host ops.schema_migrations
-- ledger by filename per house rule — one migration log, even though the view lives in
-- cognee_prod. CREATE OR REPLACE only APPENDS event_kind at the end of the select list
-- (existing columns unchanged in name/type/order), which PostgreSQL permits.

BEGIN;

CREATE OR REPLACE VIEW mimir.v_entity_freshness AS
    SELECT r.entity_id,
           e.entity_key,
           e.display_name,
           r.estate,
           max(r.last_event_at)                                        AS last_event_at,
           array_agg(DISTINCT r.source)                                AS sources,
           (array_agg(r.event_kind ORDER BY r.last_event_at DESC))[1]  AS event_kind
    FROM mimir.entity_recency r
    JOIN mimir.entities e USING (entity_id)
    GROUP BY r.entity_id, e.entity_key, e.display_name, r.estate;

GRANT SELECT ON mimir.v_entity_freshness TO cognee;

COMMIT;
