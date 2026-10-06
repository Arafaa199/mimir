-- Migration 301 — wire the stamp producers into the freshness dead-man (spec 08 ask #6).
--
-- The M365 producer is LIVE (2026-07-17) but had no dead-man: a silently-dead producer was
-- indistinguishable from a quiet mailbox — the exact failure class spec 08 exists to kill.
-- The memory API now stamps ops.data_versions `stamps_<source>` on every successful
-- POST /v1/stamps (audit.stamp_freshness; success-only, honest now(), fail-soft). This
-- registers the two domains with ops.check_data_freshness() so the existing watchdog alarms
-- when a producer goes quiet.
--
-- Thresholds:
--   stamps_m365      1800 min (30h) — producer runs 3×/day (9/13/17); overnight gap ~16h,
--                    so 30h = "missed more than a full day" = dead, not just idle.
--   stamps_imessage  2880 min (48h) — the deterministic producer is sleep-tolerant (laptop
--                    closes); 48h with no stamp = the producer is dead, not the laptop asleep.
--                    Forward-compatible: the domain does not exist in data_versions until the
--                    iMessage producer ships, and check_data_freshness only checks EXISTING
--                    domains (FROM data_versions LEFT JOIN thresholds), so this row is inert
--                    until then — no false alarm before the producer lands.
--
-- Only the thresholds VALUES list changed; the rest of the function is reproduced verbatim.

CREATE OR REPLACE FUNCTION ops.check_data_freshness()
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    v_domains JSONB;
    v_stale_count INT;
BEGIN
    WITH thresholds(domain, threshold_minutes) AS (
        VALUES
            ('finance',       1440),
            ('health',        120),
            ('nutrition',     1440),
            ('email',         1440),
            ('capture',       1440),
            ('calendar',      1440),
            ('events',        360),
            ('dashboard',     360),
            ('recurring',     1440),
            ('reminders',     1440),
            ('documents',     10080),
            ('habits',        1440),
            ('meals',         1440),
            ('approvals',     1440),
            ('agent_traces',  2880),
            ('api_usage',     1440),
            ('utility_bills', 50400),
            ('memory',        1440),
            ('nas_backup',    1800),
            ('pivpn_backup',  1800),
            ('worker_backup', 1800),
            ('db_host_backup',  1440),
            ('laptop_backup',  4320),
            ('stamps_m365',   1800),
            ('stamps_imessage', 2880)
    ),
    freshness AS (
        SELECT
            dv.domain,
            dv.last_modified_at,
            EXTRACT(EPOCH FROM (now() - dv.last_modified_at)) / 60.0 AS age_minutes,
            COALESCE(t.threshold_minutes, 1440) AS threshold_minutes
        FROM ops.data_versions dv
        LEFT JOIN thresholds t ON t.domain = dv.domain
    )
    SELECT
        jsonb_agg(
            jsonb_build_object(
                'domain', f.domain,
                'last_modified', f.last_modified_at,
                'age_minutes', round(f.age_minutes::numeric, 1),
                'threshold_minutes', f.threshold_minutes,
                'is_stale', f.age_minutes > f.threshold_minutes
            ) ORDER BY f.domain
        ),
        COUNT(*) FILTER (WHERE f.age_minutes > f.threshold_minutes)
    INTO v_domains, v_stale_count
    FROM freshness f;

    RETURN jsonb_build_object(
        'domains', COALESCE(v_domains, '[]'::jsonb),
        'stale_count', COALESCE(v_stale_count, 0),
        'checked_at', now()
    );
END;
$function$;
