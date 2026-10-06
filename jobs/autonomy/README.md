# autonomy — the trust-gated action loop (Mimir, spec 09)

First domain: **task_triage** (§7.1), running at the **propose** tier. Deterministic
producers write proposals into `ops.task_queue`; the owner approves/rejects/snoozes
from ONE daily Telegram digest; approved proposals execute audit-first and are
fully revertible. **Nothing auto-executes** — the mig-306 gate refuses every row
while the kill switch is OFF and `task_triage` sits at propose (0% trust).

Spec: [`mimir/specs/09-autonomy-loop.md`](../../specs/09-autonomy-loop.md) (§4 loop,
§5 invariants, §7.1 first-domain contract). DB: LifeOS migration `311_autonomy_task_triage`
(on top of `306_autonomy_gate`).

## The loop

```
producer_task_triage.py  (db-host timer, 03:05 UTC)   DETERMINISTIC, no LLM
  captured items (ops.task_queue: pending, plaud/claude)
   ├─ route_to_tw  ≤10/day newest-first, route_gating precision filters (owner-
   │               assigned + idle ≤7d + item_type=task + non-vague + not-already-
   │               in-TW), subject-domain→TW-project
   └─ bulk_archive_stale  ONE proposal covering the >60d backlog (ids+count+snapshot)
        → new task_queue rows status='proposed', domain='task_triage'
        → each gated by ops.can_auto_execute(...) and audited decision='proposed'
           (can_auto=FALSE for all — propose tier, 0% trust)
                        │
digest.py  (db-host timer, 03:35 UTC)
   all status in (proposed,snoozed) → ONE Telegram message via the Odin shim :3340
   grouped (routes vs bulk), each line a short id + footer: autonomy approve|reject|snooze <id>
                        │
verdict.py  (owner CLI, any host)   autonomy show|approve|reject|snooze <short-id|all> [--source]
   show    → prints the proposal + its full underlying item (source recording/transcript/quote)
   approve → status='approved' + audit 'approved'   (executor will run it)
   reject  → status='rejected' + audit 'denied'     (feeds graduation) · 'all' = every proposed row
   snooze  → status='snoozed'  (NO audit row — snooze is no signal; re-surfaces next digest)
                        │
executor_task_triage.py  (db-host timer, every 30 min)   processes status='approved' ONLY
   route_to_tw → POST tw-api :8250 (project+description+priority), resolve uuid,
                 AUDIT-FIRST finalize: audit 'executed' → proposal completed + result{uuid}
                 + revert_action 'tw_delete:<uuid>;restore_target:<tid>' + target completed
                        │
revert  (owner CLI)                       autonomy revert <short-id|uuid>
   ops.revert(task_id) stamps reverted_at + audit 'reverted' + returns revert_action,
   then the executor replays it: delete the TW task + restore the target(s).
```

The **judgment is deterministic** and lives in `autonomy-config.json` (the
subject-domain → TW-project map, the daily cap, the stale threshold, and the
`route_gating` precision filters). No LLM ranks, authors, or executes anything.

### `route_gating` — why the digest is worth reading (added 2026-07-25)

The v0 producer routed every fresh subject-mapped capture and produced a ~0%-precision
digest: the pool is ~50% `assignee=other` and dominated by stale work-meeting action
items the owner had already handled. `route_gating` (config, deterministic, tunable
without code) narrows route candidates to the ones a skeptic agrees are worth a route:

| Filter | Key | Default | Why |
|---|---|---|---|
| Owner-assigned only | `assignees` | `["self"]` | 50% of the pool is `raw_content.assignee='other'`/a named colleague — not the owner's task. The producer never checked it. `[]` = off. |
| Fresh-for-routing | `route_max_idle_days` | `7` | A separate, tight window vs the 60d **bulk** threshold. A month-old meeting item is done or abandoned; routing it is noise. Items in the 7–60d band are neither routed nor bulk-archived — they wait. |
| Real tasks | `item_types` | `["task"]` | Skip notes/observations. `[]` = off. |
| Non-vague | `min_action_chars` + `exclude_action_regexes` | `12` + meeting-verb list (`discuss`/`align on`/…) | Drop discussion fragments with no discrete action. |
| Not already in TW | `tw_dedup.enabled` | `true` (fail-open) | Don't propose routing something already an open tw-api task. On tw-api error, suppress nothing. |

These filters **only narrow candidates** — they never touch the gate, `needs_human`,
tiers, trust, or `bulk_archive_stale`. Absent config ⇒ old behaviour (a no-op).
**The pool ceiling is low regardless:** even with perfect filters only ~3–4 items in
the whole 331-item pool are fresh + owner-assigned + actionable, so **individual routes
are meant to be rare** and `bulk_archive_stale` sweeps the ancient backlog.

## Files & deploy layout (db-host, user services, `Linger=yes`)

| Repo file | Deployed to | Purpose |
|---|---|---|
| `common.py` | `~/mimir/autonomy/common.py` | psql (docker-exec on db-host / ssh fallback off-host), shim + tw-api HTTP, SQL helpers |
| `producer_task_triage.py` | `~/mimir/autonomy/` | emit proposals (deterministic) |
| `digest.py` | `~/mimir/autonomy/` | one daily Telegram digest |
| `verdict.py` | `~/mimir/autonomy/` | approve/reject/snooze CLI |
| `executor_task_triage.py` | `~/mimir/autonomy/` | execute approved + revert handler |
| `autonomy-config.json` | `~/mimir/autonomy/` | the map / cap / stale threshold (tune here, no code change) |
| `autonomy.sh` | `~/bin/autonomy.sh` | wrapper (sources `~/.config/weekly-review.env`, dispatches) |
| `systemd/autonomy-producer.{service,timer}` | `~/.config/systemd/user/` | 03:05 UTC daily |
| `systemd/autonomy-digest.{service,timer}` | `~/.config/systemd/user/` | 03:35 UTC daily |
| `systemd/autonomy-executor.{service,timer}` | `~/.config/systemd/user/` | every 30 min |
| DB objects | LifeOS migration `311_autonomy_task_triage` | statuses + `ops.autonomy_actions` + registry trigger |

Secrets come from the shared `~/.config/weekly-review.env` (`TELEGRAM_SHIM_URL`,
`TW_API_KEY`) — same file cracks-brief uses.

## Run / operate

```bash
# assemble proposals without writing (tuning workhorse)
ssh db-host '~/bin/autonomy.sh producer --dry-run --verbose'

# force a run now
ssh db-host '~/bin/autonomy.sh producer'        # emit proposals
ssh db-host '~/bin/autonomy.sh digest --force'  # resend today's digest
ssh db-host '~/bin/autonomy.sh executor --verbose'   # run approved now

# inspect a proposal + its FULL underlying item (source recording, transcript, quote)
ssh db-host '~/bin/autonomy.sh show <id>'

# owner verdicts (short id = first 8 chars, shown in the digest; or the literal 'all')
ssh db-host '~/bin/autonomy.sh approve <id>'
ssh db-host '~/bin/autonomy.sh reject  <id>'
ssh db-host '~/bin/autonomy.sh reject  all'    # every currently-proposed row (snoozed left as-is)
ssh db-host '~/bin/autonomy.sh snooze  <id>'   # re-surfaces in the next digest
#   append --source NAME to scope a verdict to one producer (default: task_triage)

# undo an executed proposal (round-trips ops.revert + deletes the TW task)
ssh db-host '~/bin/autonomy.sh revert  <id>'

# timers
ssh db-host 'systemctl --user list-timers "autonomy-*"'
```

**laptop convenience:** `verdict.py`/`executor.py` are host-portable —
`common.psql()` transparently falls back to `ssh db-host` when no local `db-host-db`
container is present. Drop `~/bin/autonomy` on laptop as `ssh db-host '~/bin/autonomy.sh "$@"'`,
or symlink the repo and run directly.

## Revert procedure (the safety currency)

`autonomy revert <id>` (or `ops.revert(task_id)` + replay). It:
1. `ops.revert()` validates (executed, not already reverted, has a revert_action),
   stamps `reverted_at`, writes the `reverted` audit row, and RETURNS the action;
2. the executor replays that action — `tw_delete:<uuid>` deletes the TaskWarrior
   task (lock-retry), `restore_target:<tid>` restores the captured item's prior
   status from `snapshot_before`.
Double-revert is refused by `ops.revert()`. Every executed proposal stores its own
`revert_action`, so any single action is undoable with one command.

## Safety posture (verify before trusting)

- **Kill switch OFF, task_triage at propose (0%)** ⇒ `ops.can_auto_execute()`
  returns false for every proposal. The producer records that verdict on every row
  (audit `decision='proposed'`, `params->>'can_auto'='false'`). Nothing auto-fires;
  execution requires an explicit owner `approve`.
- **Registry makes mislabelling unrepresentable** (mig 311 trigger): a proposal
  whose `(extracted_action, domain)` is not in `ops.autonomy_actions` raises. The
  gate's reversibility is read from the registry, never from the row.
- **Provenance = min(producer, item source)**; plaud/claude = trusted. Anything not
  listed in `provenance_by_source` defaults to untrusted (Override B holds it at
  propose forever).
- **bulk_archive_stale stays propose-only forever** (§7.1.7) — reversible but
  blast-radius-heavy; the owner drains the backlog on one decision, never the loop.

## Notes / deviations

- **tw-api uuid capture.** `POST /tasks` returns no id under `rc.verbose=nothing`,
  so the executor tags each created task `+mimir +mtq<8hex-of-target>` and resolves
  the uuid with `GET /tasks?filter=%2Bmtq…` (the `+` MUST be `%2B` — a raw `+`
  decodes to space). Zero tw-api changes; the tag doubles as provenance + a crash-
  recovery key (a re-run finds an already-created task instead of duplicating).
- **Digest is HTML-safe.** The Odin shim posts to Telegram with HTML parse mode, so
  raw `< > &` (which appear in arbitrary transcript titles) abort the send. `digest.py`
  maps them to guillemets/fullwidth-ampersand before sending — lossless to read,
  never parsed as markup.
- **Audit-first** is honoured relative to the DB state mutation: the `executed`
  audit row and the proposal/target updates commit in one transaction; the external
  TW create precedes it and is idempotent (tag recovery). Failures leave the row
  `approved` with `error` set and are retried next run (idempotent via result presence).
- **Digest shows full sentences, never truncated mid-thought.** The producer stores
  both a 90-char `title` and the full `description`; the digest renders the full text
  (cap ~300 chars — Telegram wraps) and splits into multiple messages if a batch would
  exceed the 4096-char limit, rather than cutting any item. `autonomy.sh show <id>`
  prints the complete underlying item — Plaud recording, transcript path, source_quote,
  timestamps — the "where is this information from" answer. Bulk `reject all` clears a
  whole "already handling it" batch (each row still audited individually); a zero-target
  `all` is a safe no-op.
