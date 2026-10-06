# Spec 07 — The Recall API (the "one API") + ingress registry

**STATUS: v0 BUILDING (2026-07-13).** Code: `jobs/memory-api/`.

This is **the coupling point**. Spec 05 built the spine; nothing can *read* it safely yet.
Until this exists, "one brain, one memory on every surface" is a store, not a brain.

## What it is

One service every surface calls instead of touching a store directly:

```
remember(text, estate, tags, provenance)      -> writes, with mandatory §4 provenance
recall(query, session_ctx, k)                 -> reads, scope resolved from the SESSION
```

Consumers (post-cutover): Odin `memory_save`/`memory_search`, Claude Code (via MCP),
cracks-brief, fab, Horus. They STOP talking to `memory.entries` / brain.db / cognee directly.

## The one rule that makes it safe

**Scope is a per-session capability, minted below the model, from the authenticated
transport. It is never parsed from message text and never chosen by an LLM.**

Spec 05 §4 (Fable-locked) resolves the one-brain ↔ estate-integrity collision as:
*walls at storage, bridge in the API, scope as capability.* This spec IS the bridge.

Corollaries, all enforced in code:
1. **Content may NARROW scope, never widen it.** A caller may ask for a subset of what its
   session already holds. It can never ask for more. `_narrow()` intersects; it cannot add.
2. **Scope is frozen at session creation.** `Scope` is an immutable frozen dataclass.
3. **The LLM never resolves scope.** Escalation happens via a verb parsed by *deterministic
   router code* (`parse_escalation_verb`), not by a model deciding it deserves more access.
4. **Untrusted principals never escalate.** Telegram-group members, inbound email (`From:`
   is forgeable) → single estate, propose-only, `may_escalate=False`, hard-coded.
5. **`work_confidential` is NEVER in a cross-estate scope** and NEVER returns content —
   only a pointer ("retrieve the source directly"). Its body has never reached any LLM
   (backfill registered it ledger-only) and recall must not be the hole that leaks it.
6. **Every recall is audited** to `ops.action_audit`: ingress, principal, scope, query hash,
   whether it escalated. An estate boundary you cannot prove is not a boundary.

## The ingress registry

Every surface is registered ONCE, with: `(estate, principal, auth_strength, default_scope,
sink_class, may_escalate)`. An unregistered ingress gets **no scope at all** — fail closed.

| ingress | principal | auth | default scope | sink | escalate? |
|---|---|---|---|---|---|
| `cli` / `claude_code` | owner | strong | personal + shared | owner_direct | ✅ |
| `odin_dm_owner` | owner | strong | personal + shared | owner_direct | ✅ |
| `work_cli` / `m365` | owner | strong | work + shared + **work_confidential** | owner_direct | ✅ |
| `horus` (glasses) | owner | device | personal + shared | owner_direct | ✅ |
| `odin_group` (Telegram group) | **untrusted** | weak | personal | propose_only | ❌ **never** |
| `email_inbound` | **untrusted** | none | personal | propose_only | ❌ **never** |
| `cracks_brief` (scheduled) | system | pinned | personal + work + shared | owner_direct | n/a (pinned) |
| `fab` (scheduled/voice) | owner | service | personal + shared | owner_direct | ❌ |

Escalation = owner-strong ingress + explicit verb → `{personal, work, shared}`.
**Never `work_confidential`.** Cross-estate answers go only to owner-direct sinks.

## Retrieval: no LLM at read time (design law 6)

`cognee.search(..., only_context=True)` returns the retrieved graph facts and passages
**without** an LLM completion. Recall therefore:
- costs **$0** and stays **local** (query embedding on db-host ollama),
- sends work/confidential context to **no provider** — closing the §4 "provider is a sink"
  hole at recall time, not just at cognify time,
- returns *retrieval*, and lets the calling model do the *judgment*.

Cross-estate = **fan-out to each dataset in scope + merge** (graph edges never span estates,
by design — ACL-ON gives each dataset its own vector+graph DB). Every result is checked:
if cognee returns a `dataset_name` outside the scope, that is a **leak** → raise, never return.

## Coverage (be honest)

Recall can only return what is backfilled. As of 2026-07-13: work 354/1,954 (running),
personal 575, confidential 1,977 (pointers only). **pgvector stays the recall FLOOR** —
`floor.py` is the interface; wiring `memory.entries` hybrid_search behind the same scope
rules is v0.1, and is what makes recall useful before the backfill finishes.

## Acceptance
- A scope probe proves no cross-estate leak: `recall` from `odin_group` cannot return work.
- `work_confidential` returns pointers, never content, from every ingress, always.
- An unregistered ingress gets nothing (fail closed), and it is audited.
- Escalation requires owner-strong ingress AND the deterministic verb; logged to audit.
- The LLM is not called at recall time (verify: $0 metered, no provider request).

## Fable review gate
Spec 05 §4 requires a Fable review of this layer **before reads cut over**. The scope
resolution (`scope.py`) is the security kernel — review that file, not the plumbing.
