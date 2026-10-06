# cracks-brief — "what's falling through the cracks" (Mimir flagship #1)

A once-daily Telegram brief of obligations/intentions at risk of being dropped.
Serves the owner's #1 goal and doubles as the **M1 acceptance test** (one
cross-source, cross-estate query surfaced usefully from a single job).

Spec: [`mimir/specs/01-cracks-brief.md`](../../specs/01-cracks-brief.md).
Status: **v0 live** (PG + TaskWarrior sources, deterministic scoring,
suppression, Telegram delivery via Odin, **notify-only**).

## How it works

```
insights.cracks_candidates(day)   ── ops.task_queue (tasks + plaud commitments)
   (Postgres, migration 288)      └─ finance.v_upcoming_payments (bills/renewals)
            +
tw-api :8250  (worker) ──────────── TaskWarrior pending tasks
            │
            ▼   cracks_brief.py (db-host, dependency-free: stdlib + docker exec + urllib)
  normalise (estate / ownership)      · estate = work|personal, config-driven
  → score  (Urgency+Staleness+Importance+Actionability, 0..100, NO LLM)
  → suppress (ops.cracks_dismissed: dismiss cooldown + snooze)
  → dedup   (same obligation across sources → keep highest-confidence)
  → threshold (>=45) + cap (7 total, 3/section)
  → format by horizon (Act today / This week / Heads-up / Possibly)
  → deliver (Telegram shim :3340) + log (ops.cracks_runs)
```

The **judgment lives in `cracks-config.json`** (weights, thresholds, estate map,
key-people). No LLM ranks anything. Tuning needs no code change — a cheap model
can edit the config. An optional cheap-model phrasing pass is presentation-only
(never re-ranks/adds/drops) and is **off** by default.

## Files & deploy layout (db-host, user services, `Linger=yes`)

| Repo file | Deployed to | Purpose |
|---|---|---|
| `cracks_brief.py` | `~/mimir/cracks-brief/cracks_brief.py` | the job |
| `cracks-config.json` | `~/mimir/cracks-brief/cracks-config.json` | weights/thresholds |
| `cracks-brief.sh` | `~/bin/cracks-brief.sh` | wrapper (sources env, `set -a`) |
| `systemd/cracks-brief.service` | `~/.config/systemd/user/` | oneshot |
| `systemd/cracks-brief.timer` | `~/.config/systemd/user/` | daily 03:30 UTC (07:30 Dubai) |
| DB objects | migration `288_cracks_brief` (LifeOS/backend/migrations) | fn + tables |

Secrets in `~/.config/weekly-review.env` (shared timer env): `TELEGRAM_SHIM_URL`,
`TW_API_KEY`, `OPENROUTER_API_KEY`. **tw-api note:** its `GET /tasks` summary was
extended (additive) to expose `modified/scheduled/entry/workstream`; backup at
`worker:~/tw-api/tw-api.py.bak-mimir-20260708`.

## Run it

```bash
# dry-run (assemble + print, no delivery, no log) — the tuning workhorse
ssh db-host 'set -a; . ~/.config/weekly-review.env; set +a; \
  python3 ~/mimir/cracks-brief/cracks_brief.py --dry-run --verbose'

# real run now (delivers + logs)         systemctl --user start cracks-brief.service
# override "today"                        --day 2026-07-08
# timer status                            systemctl --user list-timers cracks-brief.timer
```

## Tuning (config)

- **Too noisy?** raise `score_threshold` (45→55) or lower `caps.total`.
- **Missing transcript commitments?** set `caps.reserve_possibly: 1` to always
  hold a slot for the top "what did I say I'd do" item.
- **Wrong estate labels?** edit `estate.work_projects` / `work_content_keywords`.
- **Key people** whose replies-owed should rank max: fill `key_people.names`.
- **Payments** that are failing surface early via `keywords.urgent_importance_bump`.

## Suppression (v0 = manual; v1 = Telegram buttons)

`item_key = sha256("<source>|<source_id>")[:16]`. Find keys from the run log:

```sql
SELECT s->>'what', s->>'item_key' FROM ops.cracks_runs,
       jsonb_array_elements(surfaced) s WHERE id=(SELECT max(id) FROM ops.cracks_runs);
```

```sql
-- dismiss (hidden 7 days):
INSERT INTO ops.cracks_dismissed(item_key, source, what, reason)
VALUES ('<key>', '<source>', '<label>', 'not a crack');
-- snooze 1 week:
INSERT INTO ops.cracks_dismissed(item_key, source, what, snooze_until)
VALUES ('<key>', '<source>', '<label>', now() + interval '7 days');
```

## Acceptance test (M1 gate)

Run daily for 7 days. Success = of the top-7, owner judges **≥5 worth surfacing,
≤1 noise**, and some were things he'd otherwise have dropped. `ops.cracks_runs`
stores every surfaced item + score for surfaced-vs-acted weight tuning.

## v1 (after v0 proves precision)

Live M365 (unanswered >2 business days) + Monday due-items sources; proposed-action
buttons (approve/snooze/act) → `ops.action_audit` under the trust engine (mig 287),
all domains at 0% = propose-then-act. Nothing autonomous.
