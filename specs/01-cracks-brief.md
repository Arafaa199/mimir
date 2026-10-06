# Spec 01 — Cracks-Brief ("what's falling through the cracks")

**STATUS: ready for executor (Opus/Codex).** The architect judgments below are MADE — implement them, don't reopen. Flag back only if real schema/data contradicts one.

## Purpose
A once-daily brief of obligations and intentions at risk of being forgotten — the things you'd kick yourself for dropping. Serves the owner's #1 goal ("surface what's falling through the cracks"). Doubles as the **M1 acceptance test**: prove one cross-source, cross-estate query can be surfaced usefully from a single job.

## Definition of a "crack" (the core judgment)
An item that is ALL of:
1. an obligation or stated intention — a task, a reply owed, a payment, a commitment made aloud, a deadline;
2. time-relevant (has or implies a due/decay horizon);
3. not currently being acted on (no recent progress, not already scheduled today);
4. still savable (acting now changes the outcome).

NOT a crack: habits/wellness nudges; anything with a clear next action already on today's calendar; anything explicitly deferred/snoozed; pure FYI.

## Scope
- **v0 (build first):** Postgres-resident sources only, deterministic scoring, Telegram delivery, NOTIFY-ONLY.
- **v1 (after v0 proves precision):** add live sources (M365 unanswered, Monday due items) + proposed-action buttons (approve/snooze/act) logged to `ops.action_audit`.

## Sources & signals (verify exact columns against live schema before querying)
| Source | Where | Signal | Confidence | Ver |
|---|---|---|---|---|
| TaskWarrior | tw-api :8250 | overdue, or untouched > 7d with no scheduled date | high | v0 |
| ops.task_queue | db-host PG (mig 270) | pending/low-confidence aging past N days, or stuck in a non-terminal state | high | v0 |
| Upcoming payments | finance.v_upcoming_payments (mig 264) | bills/renewals due within 7d not marked handled | high | v0 |
| Commitments (transcripts) | capture/Plaud → ops.task_queue | extracted "I'll do X" that never became a tracked task | medium (gate) | v0 if already landing in task_queue, else v1 |
| Email / Teams | M365 MCP | thread where last msg is inbound, > 2 business days, awaiting your reply | medium (gate) | v1 |
| Monday | Monday MCP | item assigned to you, due/overdue, no recent update | high | v1 |

Estate: tag each item `work` | `personal` (estates are kept separate). The brief may mix both but MUST label estate per item.

## Scoring (deterministic — NO LLM in the ranking; the judgment lives in these weights)
Per candidate, Score 0–100 = Urgency + Staleness + Importance + Actionability:
- **Urgency 0–40:** due today/overdue ≤3d → 40; due 4–7d → 25; due 8–14d → 12; no due date → 6. Overdue > 21d DECAYS toward 0 (nagging about long-dead items is noise).
- **Staleness 0–25:** days since last activity, peaking at 3–14d (prime crack window). <1d → 0 (still fresh). >30d → 8 (likely abandoned on purpose).
- **Importance 0–25:** payments, work deadlines, replies owed to key people → 20–25; generic personal task → 10. (Key-people list = a small config the executor stubs; owner fills it.)
- **Actionability 0–10:** clear one-step resolution still possible → 10; blocked/awaiting others → 3.

## Noise control (THE critical part — precision over recall)
- Deliver ONCE daily, morning, aligned to existing timers. Use `life.dubai_today()` for "today"; ~07:30 local.
- Cap at **7 items total, max 3 per category** — never a wall of tasks.
- Suppression table `ops.cracks_dismissed(item_key text, dismissed_at timestamptz, snooze_until timestamptz)`. `item_key` = stable hash(source, source_id). Dismissed → hidden for a 7-day cooldown; "snooze 1w/1m" respected.
- Never surface an item already scheduled for today, or one below **Score threshold 45**.
- **Confidence gate:** medium/derived signals (unanswered, transcript-commitments) go in a separate "possibly" section, or are suppressed if the high-confidence list already fills the cap. Never assert a heuristic as fact (the Plaud-hallucination lesson).

## Output & delivery
- Channel: Telegram via Odin (owner's existing bot). v0 = one message.
- Format: grouped by horizon — **Act today / This week / Heads-up**. Each line: `[estate] one-line what · why surfaced (due or idle N days) · Score`. Skimmable; no essay.
- Optional phrasing pass: a CHEAP model (gemini-flash / qwen-free) MAY rewrite the assembled list into natural language — presentation only, it must NOT re-rank, add, or drop items. No frontier model in this job.

## Trust / action integration
- v0 reads only → no gating.
- v1 proposed actions map to trust domains: close/reprioritise task → `task_triage`; draft reply → `outbound_comms_{personal|work}`; calendar hold → `calendar`. All at 0% = propose. Every proposal writes `ops.action_audit(decision='proposed', trust_pct_at, tier_at)`; executes only on explicit tap → then `decision='executed', approver='user'`. Nothing autonomous.

## Architecture
- `insights.cracks_candidates(p_day date)` — SQL function returning PG-resident candidates with raw fields (source, source_id, due_at, last_activity_at, estate, importance_hint). Follow the mig-286 `insights.*` function pattern.
- A daily job (db-host systemd timer, sibling of daily-narrative / monthly-money): calls the function, (v1) merges live MCP sources, applies scoring + suppression in Python, formats, delivers via Odin `comms.send()`, logs the run.
- A config file for weights, key-people, thresholds — so tuning needs no code change (and a cheaper model can tune it).

## Acceptance test (also the M1 gate)
Run against real data for 7 days. Success = of the top-7 surfaced daily, the owner judges **≥5 genuinely worth surfacing and ≤1 noise**, AND at least a few surfaced items were things he'd otherwise have dropped. Precision over recall. Log surfaced-vs-acted to tune the weights.

## Executor decides (impl details, not judgment)
Exact SQL/column names (verify live), the timer unit, the Python structure, the config format, the Telegram rendering. Reuse existing patterns: `insights.*` functions, the daily-narrative timer, Odin `comms.send()`.

## Verify first (before writing queries)
- tw-api endpoint + fields for overdue/idle tasks.
- `ops.task_queue` status values + the aging/last-activity column.
- `finance.v_upcoming_payments` columns.
- Whether transcript-commitments already land in `ops.task_queue` (→ v0) or need a new extraction pass (→ defer to v1).
