# Spec 09 — The Autonomy Loop: acting within trust bounds

**STATUS: steps 2–4 BUILT (2026-07-20, Opus executor) — the `task_triage` loop runs at PROPOSE, dry-run live.** Migration **311** (`ops.autonomy_actions` registry + proposal-lifecycle statuses `proposed/approved/rejected/snoozed` + the trigger that makes an unregistered `(action,domain)` proposal UNREPRESENTABLE) live on db-host. producer/digest/executor deployed as db-host user timers (03:05 / 03:35 / :00,:30 UTC); verdict + revert CLIs live (`~/bin/autonomy.sh {producer|digest|executor|approve|reject|snooze|revert}`). Acceptance PROVEN end-to-end (2026-07-20): 11 proposals landed (10 route_to_tw + 1 bulk_archive_stale), ALL `needs_human`, all audited `decision='proposed'` with `can_auto=false` / `trust_pct_at=0` / `tier_at='propose'` (0 rows auto); the daily digest delivered to Telegram via the Odin shim; ONE clearly-personal route approved→executed→**reverted** (real TaskWarrior task created then deleted, target restored to pending, `reverted_at` stamped, double-revert refused) = **§7.1.8 revert-proven precondition MET**; the bulk-archive proposal rejected (232-item stale backlog left intact for the owner's own decision); producer re-run idempotent (11→11, 0 duplicates, reverted target not re-proposed). Kill switch stays OFF, trust 0% — NOTHING auto-executes; every execution needs an explicit owner `approve`. Code: `jobs/autonomy/` (+ README runbook).

**Producer precision fix (2026-07-25, Opus).** The first dry-run week measured **~0% precision** — the owner rejected the batch as "stale Plaud captures already handled." Diagnosis on live data: the v0 producer routed *every* fresh subject-mapped capture but never checked **who the item was assigned to** or whether it was *actually* fresh. The captured pool (331 pending) is **50% `assignee=other`** (164) + 28% `unknown` (93); only 15% (48) are `self`. And "fresh" used the **60-day bulk threshold** as the route window, so month-old meeting action items with expired deadlines were routed. Of the 29 standing proposals, **21 (72%) weren't the owner's task and 23 (79%) were >7d stale**; new filters would have blocked 26/29. Fix = deterministic, config-driven **`route_gating`** in `autonomy-config.json` (owner-assigned + `idle≤7d` + `item_type=task` + non-vague + tw-api dedup, all tunable, no LLM). Real-code before/after on the pool: **10→4 candidates, all 4 owner-owned**. Deployed to db-host + dry-run verified; the **emission path is byte-unchanged** — every row still lands `needs_human`/`status='proposed'` and records `can_auto=false` (40/40 historical rows proven, kill switch OFF, tier `propose`, trust 0%). **Deeper finding — it's partly the FEED, not only the producer:** even with perfect filters only **~3–4** of the 331 pooled items are fresh + owner-assigned + actionable, so **individual routes are meant to be rare** and `bulk_archive_stale` should sweep the ancient 232-item backlog (owner-decided, still intact). The 7-day §7.1.8 precision dry-run restarts on the gated producer. Remaining: that dry-run, then step 5 (graduation recommender, read-only) and step 6 (owner's first propose→bounded flip, owner-only).

**Prior — §6 items 2–4 RATIFIED + steps 0–1 BUILT (2026-07-19, Fable, fable-setup session).** Migration **306** live on db-host: kill switch (`autonomy_enabled`, ships **OFF**), trust-aware `ops.can_auto_execute` (old blind gate dropped), `ops.trust_bounds` + `within_bounds()`, `ops.revert()` bookkeeping, `'reverted'` added to the audit decision vocabulary. Truth table **24/24 green** in a rolled-back transaction (kill-switch supremacy, zero-trust-propose, per-bound refusals, both overrides, money ceilings, fabrication hard-ceiling, revert round-trip incl. double-revert refusal); live posture verified after: switch OFF, gate refuses everything. **First domain RATIFIED 2026-07-20 (Fable): `task_triage`** — bridge design in §7.1; evidence at ratification: WhatsApp still logged-out/spool-empty (no calendar feed), task_queue feed live (302 pending). Steps 2–6 are buildable at propose with the switch OFF; the owner confirms the pick at the July-24 review, and trust-flip decisions are owner-only, always.

**Grounding falsified one §2 premise:** `can_auto_execute` had **zero callers repo-wide** and 0 auto executions in 30 days — the "compatibility window" was designed for callers that don't exist, so none was built; the new gate is the only gate. First real caller arrives with the step-2 producer.

This is the third leg of the Jarvis thesis. Memory = *one brain* (done). Routing = *knows what changed* (spec 08, done). Autonomy = *acts within trust bounds* — the librarian → Jarvis leap.

## The reframe: it's 90% built and not wired together

Grounding the live estate (2026-07-18) falsified "autonomy is barely started." Both halves already exist:

- **The policy layer — trust engine (LifeOS mig 287).** `ops.trust_domain` (per-domain `max_tier` ceiling), `ops.trust_current` (per-domain `trust_pct`, set *only* by explicit owner declaration), `ops.trust_tier(domain)` → clamps trust% to an effective tier `propose | bounded | wide`. 11 domains live, all at 0% = propose today; `fabrication` and `home_security` hard-capped at propose. `ops.action_audit` is the ledger.
- **The execution layer — task_queue (mig 299).** `ops.task_queue` already carries the full propose→execute→**revert** surface: `domain, tier, triage_confidence, needs_human, status, executed_by, executed_at, snapshot_before, revert_action, reverted_at`. `fab`'s `start_print` (spec 02) is the one working end-to-end gate: audit-first, refuses without approval.

**The gap, exact and small:** `ops.can_auto_execute(p_tier, p_confidence, p_domain)` **accepts `p_domain` and ignores it** — it returns true on `tier=='auto' AND confidence>=0.20`, consulting the trust engine *not at all*. The policy layer and the execution layer have never been connected. Spec 09 is that wire, plus the loop that feeds and graduates it.

## Ownership

Architect (Fable) writes this; an executor builds it. The trust engine is the safety kernel — **every design choice below must be provable to a skeptic**, the same bar spec 05 §4 and spec 07's scope kernel were held to. When a call is ambiguous and its wrong-way cost is high, it is a Fable/owner decision, not an executor's.

## What this is NOT (guardrails)

- **NOT the LLM inventing actions.** Actions come from *deterministic producers* (§4.1). The LLM judges relevance and fills params under a schema; it never authors the action set or its own authority. (Scripts gather + tag, LLM judges — the standing discipline.)
- **NOT trust that rises on its own.** The loop *recommends* graduation; only the owner *declares* it (`trust_current`). A system that raises its own authority has no safety model.
- **NOT a per-action approval firehose.** Proposals batch into ONE daily digest (design law 2). An assistant that pings you 30×/day for approval gets muted, and a muted safety gate is no gate.
- **NOT new storage.** task_queue + action_audit + trust_domain already exist. Adding a parallel "proposals" table would be the store-sprawl anti-pattern spec 05 spent a month killing.

## §1 The tier contract (what each tier means, per action)

`effective_tier = ops.trust_tier(domain)` decides the DEFAULT path; two overrides can only tighten it, never loosen:

| Effective tier | Behaviour |
|---|---|
| **propose** | Never auto-executes. → daily digest, waits for explicit owner approval (the `fab` pattern, generalised). |
| **bounded** | Auto-executes **only** inside the domain's declared bounds (§3) AND `confidence ≥ threshold` AND the action is reversible. Outside bounds → propose. |
| **wide** | Auto-executes on `confidence ≥ threshold`. Still audited, still reversible-or-clamped, still notified after. |

**Override A — money / irreversible → always propose, regardless of tier** (design law 4). An action flagged `irreversible=true` (no `revert_action`) OR crossing a domain money ceiling re-clamps to propose even at `wide`. Trust graduates the *routine*; it never buys away the *irreversible*.

**Override B — untrusted provenance → never auto** (design law 5). A proposal whose originating ingress is `logging`/untrusted (spec 07/08 ingress registry: telegram group, inbound email) can never auto-execute, at any tier. Memory-poisoning and prompt-injection reach the world only through actions; this is where that door stays shut.

## §2 The gate (the load-bearing change)

Rewrite `ops.can_auto_execute` to consult the trust engine. Pseudocode — the real one is PL/pgSQL, `STABLE`, and unit-tested against a truth table:

```
can_auto_execute(domain, confidence, reversible, provenance_trust, crosses_money_ceiling):
    if provenance_trust != 'trusted':        return PROPOSE   # override B
    if not reversible or crosses_money_ceiling: return PROPOSE # override A
    tier = trust_tier(domain)                 # mig 287, owner-declared, ceiling-clamped
    if tier == 'propose':                     return PROPOSE
    if tier == 'bounded':
        return AUTO if (within_bounds(domain, params) and confidence >= threshold) else PROPOSE
    if tier == 'wide':
        return AUTO if confidence >= threshold else PROPOSE
```

Keep the old signature working (callers pass `p_domain` today and get the old behaviour until the new columns exist) — a **compatibility window**, not a flag day. The global `automation_config.global_confidence_threshold` (0.20) stays the confidence floor; trust is now an *additional* gate, never a looser one.

## §3 Bounds (what "bounded" means, per domain)

`bounded` is useless without declared limits. A per-domain `ops.trust_bounds` (JSONB) holds them; `within_bounds()` is deterministic. Examples the owner would declare *when* raising a domain to bounded:
- `finance_write`: `{max_amount_aed: 500, payees: [known recurring only], per_day_cap: 1000}` — auto-pay known recurring utility/telecom/subscription bills under the cap; anything larger or to a new payee → propose.
- `calendar`: `{sources: [imessage_commitment, m365], no_conflicts: true}` — auto-create an event only from a detected commitment with no calendar clash.
- `task_triage`: `{route_only: true}` — file/route a captured task; never close or delete one.

Bounds are themselves owner-declared config, versioned in `trust_policy_log`. An empty bounds set at `bounded` = behaves as propose (fail-safe).

## §4 The loop

**4.1 Produce.** Deterministic producers write candidate rows into `ops.task_queue` (status `proposed`), each carrying `domain, action, params, reversible, revert_action, idempotency_key, provenance_ingress`. Sources already exist: `cracks-brief` candidates (spec 01), routing producers' commitments (spec 08 — an iMessage "I'll send you X" → a proposed task), recurring-bill due dates (finance). The LLM may *fill or rank* params under a schema; it does not author the action list.

**4.2 Gate.** §2 decides `auto` vs `needs_human` per row, writing `trust_pct_at`/`tier_at` into `action_audit` at decision time (so a later trust change never rewrites history).

**4.3 Propose.** `needs_human` rows collect into ONE **daily digest** (reuse cracks-brief's delivery rail — Telegram/Odin), each with approve / reject / snooze. Approve → execute; reject → close + feed graduation stats; snooze → re-surface. Owner-direct sink only (spec 08: personal context never lands in a group).

**4.4 Execute.** **Audit-first** (the `fab` invariant): write `action_audit` decision+intent BEFORE the side effect, capture `snapshot_before`, execute through the domain's own executor, record `result` + `executed_at`/`executed_by`. `idempotency_key` makes a double-fire a no-op. Execution lives in exactly one place per domain, never in an LLM-reachable tool (spec 02's Kiln lesson).

**4.5 Revert.** Every executed row stores `revert_action`; `ops.revert(task_id)` replays it and stamps `reverted_at`. Reversibility is the currency that lets an action rise above propose — so the revert path is tested *before* the domain is ever raised, not after.

**4.6 Graduate.** The loop **never writes `trust_current`.** It computes, per domain, an approval record from `action_audit` (approved / rejected / reverted over a rolling window) and, when a domain clears a bar (e.g. ≥10 proposals, ≥95% approved, 0 reverts, ≥2 weeks), emits a *recommendation* into the daily digest: "*Raise `calendar` propose→bounded? 14/14 approved, 0 reverts, 3 weeks.*" The owner declares the raise (a one-line `trust_current` update, logged to `trust_policy_log`). This is the ratchet: **weeks of proven correct proposals, then an explicit human flip** — never an inference.

## §5 Safety invariants (the never-list)

1. **Trust rises only by explicit owner declaration.** The loop recommends; the owner declares. No code path writes `trust_current`.
2. **Money / irreversible = highest approval, always** (override A) — independent of trust level.
3. **Untrusted-ingress proposals never auto-execute** (override B) — the injection firebreak.
4. **Audit-first, idempotent, reversible-or-clamped.** No side effect precedes its audit row; no action auto-executes without a tested `revert_action`.
5. **Global kill switch.** `automation_config.autonomy_enabled=false` drops *every* domain to propose instantly (one write, estate-wide). Ship this in the *first* commit, before the first producer.
6. **One batched digest/day.** Never a per-action approval stream (design law 2).
7. **Hard ceilings hold.** `fabrication` and `home_security` stay `max_tier=propose` forever; no trust% can raise them. Physical/security actuators never graduate.

## §6 What Fable must ratify before build (RATIFIED — items 2–4 on 2026-07-19, item 1 on 2026-07-20)

1. **First domain** (§7) — **RATIFIED 2026-07-20: `task_triage`** (§7.1). The deciding fact was feed reality: calendar's feed (WhatsApp commitments) measured nonexistent; task_triage's measured live.
2. **The graduation bar — RATIFIED, tightened from draft.** A domain may be *recommended* for propose→bounded only when ALL hold in a **rolling 30-day window**: ≥10 decided proposals · ≥95% approved · **0 reverts ever in the domain** · **no rejection among the 5 most recent decisions** (an old approval pile must not carry a recently-wrong domain over the line) · ≥14 days since the window's first proposal · **the domain's revert path proven** (≥1 successful `ops.revert()` round-trip on its action shape — §4.5's "tested before raised" made a precondition, not a hope). **Snoozes are no signal** — they count in neither numerator nor denominator. Owner tunes only by explicit declaration.
3. **`bounded` bounds schema — RATIFIED as implemented (mig 306 `ops.trust_bounds` + `within_bounds()`), finance_write + calendar only.** `finance_write`: `{max_amount_aed, daily_cap_aed, payees_mode:"known_recurring_only"}` where the payee test is an **exact `finance.recurring_items` id reference against an active row — never fuzzy merchant matching** (a matcher that guesses a payee is how money leaves by accident). `calendar`: `{sources:[...], require_no_conflict, max_duration_min}` (duration cap added — a "commitment" that books a whole day proposes). Universal rule, enforced in code: **a constrained dimension missing from params ⇒ out of bounds** — an action cannot dodge a bound by omitting the field it constrains; empty/absent bounds at bounded tier ⇒ propose.
4. **Money ceiling — RATIFIED at 750 AED single / 1,500 AED daily aggregate** (config keys `autonomy_money_ceiling_*_aed`). The draft's 200/1000 contradicted its own §3 example (a 500-AED bounded cap above a 200-AED absolute ceiling is unreachable) and the stated payoff (the largest known recurring bill is **593 AED** per the mig-302 reconciliation). The ceiling is the LAST line, not the first: nothing autos below it either until a domain graduates and its own tighter bounds pass. Owner may lower by one config write.

**Kill-switch semantics (ratified):** `autonomy_enabled` ships **OFF**; absent or malformed row = OFF (fail-closed, the gate string-compares jsonb `true`). Arming it is part of the owner's first step-6 flip — between now and then even a gate bug cannot auto-execute anything.

## §7 Candidate first-domains (pick after July-24 dry-runs)

Prove the *mechanism* on something reversible and low-stakes; earn trust; only then point it at money.

| Domain | Why / why-not first | Stakes |
|---|---|---|
| **calendar** | Auto-create an event from an iMessage/M365-detected commitment, no-conflict only. Reversible (delete the event). **Compelling IF the iMessage-commitment dry-run proves accurate** — which is exactly what July 24 tells us. | low |
| **task_triage** | Auto-file/route captured tasks (route-only, never close). Highest-frequency, most-reversible, best pure mechanism-prover. | low |
| **knowledge_write** | Auto-`remember()` high-confidence facts. Reversible (delete the row). Ties to the spec-07 write path. | low |
| **finance_write (bounded)** | The real payoff: auto-pay known recurring bills under a cap to known payees — well-grounded by the mig-302 reconciliation views + registry. **Not first** — money graduates *after* a reversible domain proves the loop and earns trust. | high |
| ~~outbound_comms~~ | Publishes to third parties (spec 08: the reply IS the leak). Not an early autonomy domain. | high |
| ~~fabrication / home_security~~ | Hard-capped at propose forever (§5.7). | physical |

**Recommendation:** first loop = **calendar** or **task_triage** (whichever the bake shows better-fed), to prove propose→approve→execute→revert→graduate end to end on something you can undo with one tap. Then **finance_write bounded** as the first high-value loop, once the mechanism is trusted and a reversible domain has walked the full graduation ratchet at least once.

**→ Resolved: task_triage.** The feed data answered "whichever better-fed" — see §7.1.

### §7.1 First domain: task_triage — RATIFIED (2026-07-20, Fable) + the trust-vocab bridge

Evidence at ratification (live-measured, not recited): the WhatsApp bridge reports `connection:"disconnected"` / "Logged out — re-scan QR" with a 0-byte spool ⇒ **calendar's commitment feed still does not exist**. `ops.task_queue` holds 302 pending captured items (297 plaud, 6 claude) with continuing inflow ⇒ **task_triage's feed is live today**. Sequence: calendar second (after WhatsApp re-pair + a clean semantic review week), finance_write bounded third (after one full graduation ratchet on a reversible domain).

**The vocabulary bridge** — `task_queue.domain` speaks triage-subject vocabulary (`personal/work/finance/lifeos/infra/health`) while the trust engine speaks action vocabulary (`task_triage/calendar/finance_write/…`). Resolution, ratified:

1. **Proposal rows carry TRUST-vocab in `task_queue.domain`.** Autonomy proposals are NEW rows whose `domain` is the trust-engine action class; captured items keep their subject labels and are only ever the *targets* of proposals. No parallel column, no row migration. Why this way: mig 306's money-cap queries already read `task_queue.domain='finance_write'` on executed rows — the schema has chosen; and subject-vocab rows can never leak into auto-execution because gate check #2 refuses any domain not registered in `ops.trust_domain` (truth-table-covered). Analytics discriminate by `status='proposed'` + `source='autonomy-<producer>'`.
2. **`ops.autonomy_actions` registry — domain and reversibility are DERIVED, never self-declared.**
   ```sql
   CREATE TABLE ops.autonomy_actions (
     action      text PRIMARY KEY,
     domain      text NOT NULL REFERENCES ops.trust_domain(domain),
     reversible  boolean NOT NULL,
     description text NOT NULL
   );
   -- v0 seed (task_triage): route_to_tw · archive_noise · bulk_archive_stale (all reversible)
   ```
   A BEFORE INSERT/UPDATE trigger on `task_queue`, scoped `WHEN (NEW.status = 'proposed')`, requires `(extracted_action, domain)` to exist in the registry — a proposal claiming a domain its action doesn't belong to is *unrepresentable*. The gate's `p_reversible` is read from the registry, not the row. Each domain's executor holds a closed action allow-list (dispatch by domain; unknown action ⇒ refuse + audit). A buggy or compromised producer that mislabels therefore buys nothing at either layer — the same bad-config-is-unrepresentable bar as spec 07's scope kernel.
3. **Provenance = min(producer, item source).** The producing job (systemd, owner infra) is `trusted`; a proposal inherits the WEAKER of that and its underlying item's authoring source: `plaud`/`claude` (owner voice/session) → `trusted`; anything imessage/whatsapp-derived → `untrusted` until that semantic layer passes its review bar — Override B then holds those at propose regardless of domain trust. The map is a producer constant; reclassifying a source is a deliberate edit, never an inference.
4. **Status vocabulary.** The step-2 migration extends the `task_queue` status CHECK with the proposal lifecycle: `proposed / approved / rejected / snoozed` (execution reuses `in_progress/completed/failed/reverted`; archived *targets* reuse existing `skipped`). Idempotency needs no new machinery: the existing unique `(source, source_id)` index with `source_id = '<action>:<target_id>'`.
5. **v0 producer is deterministic — no LLM.** Triage (qwen) already labeled every item; the producer derives routes from those labels (subject-domain → TW-project map, priority passthrough), emits ≤10 proposals/day newest-first, plus ONE `bulk_archive_stale` proposal covering the ancient backlog (~200 items idle 77–114d) so the swamp drains on a single owner decision instead of poisoning weeks of digests. Rejected proposals never re-emit (source_id dedup + `rejected` status).
6. **Executor at propose (step 4).** `route_to_tw` → tw-api :8250, TW uuid captured into `result`, `revert_action` = delete-that-uuid + restore target status from `snapshot_before`. `archive_noise` → target `status='skipped'`, revert = restore. Audit-first, the fab invariant.
7. **`within_bounds` evaluator for task_triage is deliberately deferred to the graduation migration** — mig 306 already fails an evaluator-less bounded domain to propose, which is correct until the owner declares bounds. Reserved shape: `{route_only: true, max_actions_per_day: N}`; `bulk_archive_stale` stays propose-only forever (reversible but blast-radius-heavy).
8. **Step-4 dry-run bar (ratified):** ≥7 days at propose · ≥90% of non-bulk proposals approved · ≤1 wrong-route · ≥1 real `ops.revert()` round-trip on an approved-and-executed row. Only then is a graduation recommendation even eligible (§6.2's bar unchanged on top).

## §8 Build order

| # | Step | Gate |
|---|---|---|
| 0 | Kill switch (`autonomy_enabled`) + trust-aware `can_auto_execute` rewrite (compat window) + unit truth-table | tests green; old callers unchanged |
| 1 | `ops.trust_bounds` + `within_bounds()` + `ops.revert()` | revert proven on a synthetic executed row |
| 2 | ONE producer → task_queue `proposed` (the §7 pick's source) | proposals land, gated correctly, **0 auto at 0% trust** |
| 3 | Daily digest (approve/reject/snooze on cracks rail) | round-trips; owner-direct sink only |
| 4 | First domain executor + audit-first + revert, at **propose** | dry-run: N proposals, owner judges precision; nothing auto-fires |
| 5 | Graduation recommender (read-only stats → digest) | recommends only after the bar; never writes trust_current |
| 6 | Owner declares first propose→bounded flip; watch the first *auto* actions | every auto action reversible + audited; kill switch drops it instantly |

Steps 0–1 are pure convergence (wire what exists). Step 4 runs at propose for its own dry-run week — the mig-297 law, applied to *actions*: never let a matcher act on real state without a dry run, and an action has higher stakes than a stamp.

Steps 2–4 are unblocked by the §7.1 ratification and safe to build immediately: the kill switch is OFF and all trust is 0%, so every proposal lands `needs_human` — the build's worst case is a digest. Step 6 stays owner-only.

---

**One-line thesis:** the trust engine says *how much* you may act per domain, the task_queue says *how* to act-and-undo, and spec 09 is the wire between them plus the weekly ratchet that turns approved proposals into earned autonomy — with money and the irreversible always staying behind your explicit yes.
