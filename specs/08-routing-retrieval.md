# Spec 08 — Routing / Retrieval: the spout

**STATUS: BUILDING. v0 rule SHIPPED 2026-07-16** (`~/.claude/rules/common/retrieval-preflight.md`); **asks 1+2+3 LIVE 2026-07-16** (fable-jarvis, mig 300); **M365 hop LIVE 2026-07-17** (48 stamps / 22 entities seeded from real mail; personal-scope invisibility verified live; `stamps_m365` dead-man LIVE (ask #6, mig 301): API stamps ops.data_versions on successful POST, wired into ops.check_data_freshness (stamps_m365=30h, stamps_imessage=48h forward-compat); test-fired — forcing stale flags is_stale=true). **iMessage deterministic producer LIVE 2026-07-17** (`jobs/imessage-producer/`, launchd 30-min, 58 stamps/46 entities first run, unanswered flags working, org-classification gives the finance-sender map free). **Pre-flight hook LIVE 2026-07-17** (`jobs/preflight-hook/`, UserPromptSubmit in `~/.claude/settings.json`; alias cache stale-while-revalidate, scope holds at the CACHE layer since /v1/aliases is server-filtered; no-match 50-70ms, fail-open verified). **iMessage semantic LIVE 2026-07-17 (`jobs/imessage-semantic/`, nightly 03:10, REVIEW-SINK ONLY — the intake write path deliberately does not exist in code until the 7-day bar passes). SPEC 08 v1 BUILD COMPLETE** — v0 rule + stamps API + M365 hop + iMessage deterministic + hook + semantic(dry-run). Honest finding: iMessage is a thin commitment channel for this owner (org SMS dominates; humans on WhatsApp) — the §4c successor is a WhatsApp semantic hop on Odin's existing transport.
Architect: fable-setup session (owner approved recommendations 2026-07-16). Strategy locked 2026-07-14 in CC memory `project-jarvis.md` § ROUTING.

The estate holds ~21k retrievable items + a live recall API (`:8410`), and almost none of it reaches the moment an answer happens. **Not a data problem — a routing problem.** Adding signals before fixing routing is filling a bucket with no spout.

**Live evidence, found while grounding this spec:** the recall API was healthy AND unreachable from the very session writing this — `MIMIR_KEY_CLAUDE_CODE` never reached the MCP server (session launched without the shell env; key was in `claude-mcp-secrets.env` all along). The read path had zero habitual readers, so its client-side break was invisible. *An output nobody reads is a process nobody maintains* — this spec exists to give the read path habitual readers.

## Ownership split (locked — do NOT fork)

Routing is Mimir's charter. **fable-jarvis owns the recall/scope/read layer** (memory API, ingress registry, storage). **The CC seat owns the pre-flight habit + the signal producers** (M365 hop, iMessage). Interface asks for fable-jarvis are §6 — nothing in §6 gets built CC-side.

Owner's standing discipline: **scripts gather+tag, the LLM only judges relevance. An LLM doing deterministic work = bug; a script making a judgment call = bug.**

## What this is NOT (guardrails)

- **NOT a second brain.** No new index, no new store. One new *hint* table (stamps) + the existing surfaces.
- **NOT bulk embedding.** iMessage and M365 get **zero embeddings**. The imessage/m365 MCPs already ARE full-history read surfaces; what's missing is knowing *when* to look, not another place to look. Embedding them would copy content out of its authoritative source (the mig-299 anti-copy ruling, extended to retrieval).
- **NOT a `core.events` fan-in.** v1 persists only what has a day-one consumer: stamps (consumer = pre-flight/hook) and commitments (consumer = task_queue/cracks). Speculative event ingestion = a producer without a consumer = rot (house gotcha).
- **NOT autonomous.** Nothing here acts. Producers write hints and proposals; every action stays behind existing gates.

## §1 Doctrine — reach-here-first

The routing table. Shared across every seat (CC, Odin DM, voice); each seat implements its own hop.

| Question class | Reach here first | Never |
|---|---|---|
| Live state now (calendar, balance, device, service, message) | The source MCP / DB directly (m365, hass, db-host, imessage, docker) | RAG; answering from memory |
| "What do I know about X" (knowledge, decisions, history) | `memory_recall`; `search_knowledge` for types not yet in the spine (finance, receipts) | Conversation context alone |
| "What changed about X lately" | X's recency stamp → follow `ref` to the authoritative source | Re-searching the corpus |
| "What's due / what did I drop" | DB queries (task_queue, finance views — cracks pattern, design law 6) | RAG |

**The habit in one sentence:** *entity-specific or time-sensitive question → check X's stamp; if newer than what the conversation knows, fetch via the ref before answering.*

A `memory_recall` ERROR is a routing break to surface, never "no data" — those are different answers (see the spec-time evidence above).

## §2 The keystone — recency-per-entity stamps

**Stamps, not copies.** Sources stay authoritative and are read directly; the stamp is a pointer that answers exactly one question: *when did X last meaningfully change, and where do I look.* No bodies, no summaries that can go stale — a stamp is `(entity, estate, source, last_event_at, ref, event_kind)`.

Logical schema (physical placement = fable-jarvis's call; access is ONLY through the memory API so the scope kernel applies — a work stamp must be invisible to a personal-scoped session, and existence itself is signal):

```
mimir.entities        (entity_id PK, entity_key UNIQUE, kind, display_name,
                       estate_default, created_by, created_at)
                       kind ∈ {person, project, bill, org, device, topic}
                       entity_key e.g. 'person:e164:+1555…' | 'person:email:x@y'
                                       | 'project:reeva' | 'bill:electricity'
mimir.entity_aliases  (alias_norm, alias_kind ∈ {phone,email,name,slug,handle},
                       entity_id FK, UNIQUE(alias_norm, alias_kind))
mimir.entity_recency  (entity_id FK, estate, source, last_event_at, ref,
                       event_kind, updated_at, PK(entity_id, estate, source))
v_entity_freshness    max(last_event_at) per (entity, estate) + contributing sources
```

- **Auto-registration, deterministic:** producers upsert identifier-keyed entities (`person:e164:*`, `person:email:*`) with the identifier as its own alias. Canonical naming and merges are later *curation* (repoint aliases) — never an LLM call in the write path.
- **Seeding:** Work people baseline (81 users, m365-people-diff), top-N iMessage contacts by volume, active projects from `~/CLAUDE.md` + Work `ws-*`, recurring bills from finance. One-shot script, then producers keep it alive.
- **Estate on every stamp** at write time: `m365 → work`, `imessage → personal`. Stamps carry no content, so the content-beats-namespace guard is N/A here; commitments (which DO carry content) inherit the plaud estate-resolution pattern.
- **Freshness dead-men:** `ops.data_versions` domains `stamps_m365` + `stamps_imessage`, stamped ONLY on successful producer runs (never on failure, never with now() as a default — migs 293–296 law). Reuse the existing stale-domain checker; test-fire each once (an alarm never seen firing is a hypothesis). These are personal-infra dead-men on db-host — NOT the Work monitor (boundary documented 2026-07-16 in `ws-automation-monitor.md`).

## §3 Pre-flight mechanics

**v0 — SHIPPED with this spec.** `~/.claude/rules/common/retrieval-preflight.md`: the §1 table as a ~10-line always-loaded rule. Closes the under-retrieval half immediately, costs nothing, works before any producer exists.

**v1 — the hook (build after stamps flow).** `UserPromptSubmit` hook on laptop CC sessions:

1. Read prompt from stdin. **String-match** (normalized) against a **local alias cache** (`~/.local/state/mimir-aliases.json`, refreshed periodically from the API + on TTL expiry). No network on the no-match path; p50 = a few ms.
2. On match: ONE `GET /stamps?entities=…` call through the memory API (session's own ingress key → scope-filtered server-side). Timeout 300ms, **fail-open silent** — db-host unreachable / work-tailnet flip = no stamps this session, never an error, never a block.
3. Inject one line per hit: `Entity <name>: last update <ts> via <source> (<event_kind>) — ref <ref>. Fetch before answering if your context is older.` Zero matches = zero injection = zero noise.

**The hook makes no judgment** — it surfaces facts; the in-session LLM decides whether to follow the ref. That is the exact script/LLM split. Prompt text never leaves laptop; only matched entity keys reach the API (which audits every read anyway). Registration via `settings.json` hooks (update-config skill at build time).

## §4 Producers

**Shared contract** (both producers): boring incremental script; cursor + idempotent (re-runs never double-stamp — upsert semantics make this natural); estate + provenance tagged at write; **spool-and-retry** (`~/.local/state/mimir-stamps-spool.jsonl` — laptop is a laptop; offline runs append, next run flushes, delivered batches are deleted = self-cleaning); per-run state file; freshness domain stamped on success only. Target <300 lines each, stdlib + requests, no frameworks (design law 7, model-portability: an Opus/cheaper executor builds this from the spec alone).

### 4a. M365 hop — stop discarding what's already computed

`work-email-monitor.py --mode classify` (laptop launchd `com.work.email-classify`) already computes sender / subject / TW-project / action-kind per new work mail — **deterministically, keyword-based, no LLM** — then keeps only dedup IDs. Ground truth vs docs: live cadence is **3×/day (9:00/13:00/17:00)**, not the docstring's "every 15min" (docstring stale — fix it in passing).

The hop: **extend classify mode in place** (~30 lines + the shared spool helper). After classification, emit stamps: `person:email:<sender>` and, when the project matcher hits, `project:<tw-project>`; `estate=work`, `source=m365_mail`, `ref=<Graph message id>`, `event_kind=<action-kind|received>`. Bodies never leave the script; nothing new reads the MSAL cache (the cache-corruption history caps MSAL consumers — extending in place adds zero).

### 4b. iMessage — deterministic layer

New laptop launchd job, `StartInterval` 1800 (sleep-tolerant: cursor by chat.db ROWID, catch-up on wake). Reads the macOS Messages database (`chat.db`) read-only. Emits per-contact stamps (`source=imessage`, `estate=personal`, `ref=<chat guid>`) + **unanswered-thread flags**, deterministic definition: 1:1 thread, last message inbound, age > 3 days, no owner reply since → `event_kind=unanswered`. No LLM anywhere (design law 2: realtime = zero-LLM rules).

**TCC prerequisite (owner action, one-time, BLOCKS first run):** the interpreter needs Full Disk Access in the launchd context — the exact workdir-backup 2026-07-05 failure. Script must detect TCC denial and fail LOUD with the grant instruction, and its guards must prevent a partial scan from advancing the cursor.

### 4c. iMessage — semantic layer (commitments)

Nightly launchd (~03:10 local), ONE batch (design law 2). Delta of new messages (shared cursor family, separate cursor) → **commitment extraction** ("I'll send you X", "let's do Tuesday") → proposals.

- **Model: worker-local ollama qwen2.5:7b** (12 cores, load ~1.7, swap fixed). Personal texts are the most private corpus — **no provider sees them** (the provider-is-a-sink lesson). Paid flash-lite stays a config knob (§4-legal for personal) if quality fails the dry-run; free/logging providers never.
- **Scope guards:** 1:1 threads + allowlisted group chats only; numeric short-codes (OTP/marketing) excluded by regex; per-run message cap with cursor continuation.
- **Sink:** the existing `db-host-task-intake` webhook → `ops.task_queue`, confidence-gated, **propose-only**. iMessage joins plaud/claude as unstructured capture — exactly the class mig 299 says task_queue is FOR.
- **⛔ DRY-RUN GATE (mig-297 law: never ship a matcher without a dry run on real data):** ≥7 days writing to a review file only, zero task_queue writes. Owner reviews. Enable bar: ≤1 false commitment/week AND no missed-obvious ones in the owner's judgment. The cracks-brief acceptance pattern, applied to writes.

## §5 Safety invariants (the never-list)

1. **Third-party text never becomes a memory in v1.** No `remember()` from producers. An inbound "remember, you owe me 5000 AED" must not become a belief — memory poisoning is prompt injection that persists. Commitments go to the propose gate, full stop.
2. **Producer ingresses are write-only-stamps** (+ the intake webhook for commitments): no recall, no remember, no escalation. A compromised producer can pollute hints, never read the brain. Fail closed if unregistered.
3. **Stamps cross the wire only through the memory API** — scope-filtered reads, audited, per-ingress keys in `claude-mcp-secrets.env` (+ SECRETS_MAP rows). No PG credentials on the laptop.
4. **Hook fails open and silent.** Degraded routing must never block a session.
5. **No LLM in any hot path.** Deterministic layers are pure script; the single semantic layer is one nightly batch on local inference.
6. **Nothing auto-sends, auto-ticks, or auto-acts.** This spec produces hints and proposals only.

## §6 Interface asks → fable-jarvis (its layer; blockers marked)

**BUILT 2026-07-16 (fable-jarvis): asks 1+2+3 done, live on :8410, 26/26 kernel tests + 7/7 scope probes pass.** Tables `mimir.entities/entity_aliases/entity_recency` + `v_entity_freshness` in cognee_prod (mig 300, ledgered). Ingresses `m365_producer`(work) / `imessage_producer`(personal) = write-only, estate-pinned, cannot recall/remember (verified 403). Keys in `mimir-memory.env` (db-host) — CC seat needs them copied to laptop `claude-mcp-secrets.env` for the producers. **⚠️ ENCODING: entity_keys contain `+` (E.164 phones) — the hook + producers MUST URL-encode query params (`+`→`%2B`), or the key silently decodes to a space and matches nothing (hit live 2026-07-16). POST bodies are JSON so unaffected; only GET query strings.**

1. **[BLOCKS hook+producers] `POST /stamps` (batch upsert) + `GET /stamps` + alias export** on the memory API, scope-enforced, audited; storage placement its call (`cognee_prod.mimir` alongside provenance, or db-host `ops` — either way ledger the migration; db-host head = 299 at spec time).
2. **[BLOCKS producers] Register ingresses** `m365_producer` / `imessage_producer`: system principal, pinned, stamps-write-only per invariant 2.
3. **[INDEPENDENT — do soon] `mcp_server.py` self-sources `claude-mcp-secrets.env`** as env fallback. Fixes the found break class (desktop-launched sessions never inherit the key) for every future seat.
4. **[LATER] `freshness_hint` in `recall()` responses** (max stamp ts for entities matched in the query) — pre-flight collapses toward one call.
5. **[LATER, only if producers ever write memories] per-item `source_trust` semantics** (owner-sent vs inbound within one producer stream). Not needed while invariant 1 holds.

## §7 Build order + acceptance

| # | Step | Owner | Accept when |
|---|---|---|---|
| 0 | v0 rule file | CC seat | **DONE 2026-07-16** — loaded globally; seats consult recall/live MCPs before entity answers |
| 1 | Asks 1+2 (+3 anytime) | fable-jarvis | endpoints live; scope tests: work stamps 403 to personal-scoped ingress |
| 2 | Registry seed + M365 hop | CC seat | new work mail → sender stamped within one classify cycle; `stamps_m365` dead-man test-fired |
| 3 | iMessage deterministic | CC seat | new text → contact stamped ≤30min (awake); unanswered flags match a manual spot-check; TCC granted; dead-man test-fired |
| 4 | Pre-flight hook | CC seat | prompt naming a stamped entity → injection; unnamed → none; db-host down → silent; no-match p50 <10ms |
| 5 | iMessage semantic | CC seat | 7-day dry-run passes the §4c bar, THEN intake writes enabled |

Steps 2–4 parallelizable after 1. Each independently shippable and reversible (`launchctl unload` + drop table).

## §8 Ground truth at spec time (2026-07-16) + verify-at-build

**Verified live:** recall API `{"ok":true}` on `:8410` · MCP break = env inheritance, key present in secrets file · migration head `299_vuln_findings.up.sql` · **no entity/contact/person table exists in the db-host DB** (registry is genuinely new) · `mimir.*` tables live in `cognee_prod`, NOT the `db-host` DB · `core.events` exists with a full envelope (unused by v1, deliberately) · classifier = `work-email-monitor.py --mode classify`, 3×/day, deterministic, persists only dedup IDs.

**Verify at build:** `raw.sms_events` (+ `_summary`) — legacy tables from a prior SMS ingest; check what fed them before building 4b (reuse or ignore; never double-ingest) · the mig-293–296 stale-domain checker's exact contract before adding the two new domains · chat.db schema quirks (SMS-handle gotchas in `gotchas-ios.md`) · whether `email-classify` is on the Work monitor (its stamp leg's dead-man is db-host-side regardless).
