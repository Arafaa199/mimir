# Spec 05 — Memory Spine Phase 2: migrate onto cognee (THE coupling)

**STATUS: ready for executor — its own focused session. This is a PRODUCTION memory migration.** Phase 1 (spec 03) chose cognee (+71% on relational+temporal, controlled: cognee-GRAPH beat an A+RAG control +300%, so it's the graph, not the LLM). Phase 2 makes cognee the ONE spine every surface reads/writes — this is what turns the four capabilities from islands into one brain.

> **⚠️ TOPOLOGY MOVED 2026-07-11 — read the (private) migration handoff before acting.** The backfill was never actually running: both cognee endpoints (claude-shim :8088 + ollama :11434) lived on **worker, a laptop that sleeps**. When it napped mid-run every doc failed `Errno 101`, the run transient-skipped all 5,451, **exited 0**, and nothing alerted — silently stuck at 25/5451 for a day. Both halves are now **db-host-local** (shim 127.0.0.1:8088, loopback-bound on purpose — it is an unauthenticated proxy to the Claude subscription; ollama localhost:11434, which binds its LAN IP, not loopback). Embedding space **re-measured, not assumed** (`check_embed_space.py` → cos=1.000000; db-host runs ollama 0.13.5 vs worker 0.16.1 — **laptop at 0.31.1 is still incompatible at 0.826, never embed the shared store from laptop**). Added a `BackendDown` circuit breaker + `OnFailure=` pager so a dead backend can never again be a silent no-op. **LLM backend is `claude-shim`, NOT OpenRouter** despite what older docs say (`run_on_db_host.sh:32`); OpenRouter is a one-line fallback and its key now DOES work on paid models ($9.92 of $20 left vs ~$29 for a full run).

**Grounding (read first):** `jobs/memory-spine-bakeoff/report.md` + CC memory `project-memory-spine-bakeoff.md` (working cognee config + gotchas, incl. the cognee↔ollama-nomic 422).

## Goal
Collapse 4 stores / 3 embedding spaces into ONE spine: cognee (vectors + graph in one store), nomic-768 embeddings, personal/work/shared namespaces, a single read/write API every capability + Odin uses. Keep pgvector-hybrid as the recall FLOOR. Retire the others as separate indexes — but never delete their data.

## Non-negotiable safety (this touches production memory)
1. **Staged, reversible — never a big-bang cutover.** Phases: (a) stand up cognee prod on db-host; (b) BACKFILL existing memory (`memory.entries` + brain.db + `search.embeddings`) into it, non-destructively; (c) SHADOW-WRITE — new writes go to BOTH old stores and cognee for a soak period; (d) VERIFY parity (cognee recall ≥ old on a held-out cross-estate query set); (e) CUTOVER reads to cognee; (f) only after a clean soak, stop writing to the old paths — do NOT drop the old data, keep it cold.
2. **pgvector stays the recall floor** even post-cutover. The bake-off was n=7 docs / 16 queries — directionally strong but small; production is bigger and messier. The floor is the insurance for that uncertainty, and it makes the whole migration recoverable.
3. **Fix the cognee↔ollama-nomic 422 FIRST** (bake-off gotcha). The embedding path must be solid before backfill.
4. **Estate boundary (LOCKED 2026-07-10, Fable adversarial review — resolves one-brain ↔ estate-integrity).**
   **Walls live at STORAGE; the bridge lives in the API; scope is a per-session capability.** Rejected: hard isolation at query (kills one-brain) AND "tag everything, enforce at output" (output filtering cannot hold — paraphrase/inference laundering, tool-call arguments, multi-turn context residue, memory write-back contamination, and the inference provider itself all leak before or around any output filter).
   - **Storage: cognee ACL ON, per-estate datasets** `personal` / `work` / `shared` (+ `work_confidential`). ACL-ON datasets are cognee's only measured-enforced primitive (ACL off ignores `datasets=[...]` — leak measured in stage A), and physical partitioning makes estate purge provable (Work offboarding). Accepted cost: graph edges never span estates — cross-estate joins happen at answer time over fan-out results, not by graph traversal. Revisit only if a real query class fails on it.
   - **One brain = the memory API, not the store.** `recall(query, scope)` where scope ⊆ {personal, work, shared, work_confidential} is resolved by the API from the session — never parsed from message text, never chosen by the LLM. Cross-estate = fan-out to both datasets + merge.
   - **Scope minting (ingress registry).** Every ingress registered with (estate, principal, auth strength, default scope, sink class). Work surface → {work, shared}; personal surface → {personal, shared}; untrusted-principal ingress (Telegram group, inbound email — From: is forgeable) → single estate, propose-only, NO escalation ever. Owner-strong surfaces (CLI, paired app, owner-ID DM) may escalate to cross-estate via an explicit verb parsed by deterministic router code (not the LLM), logged to `ops.action_audit`. **Content may narrow scope, never widen it. Scope is fixed at session creation.** Scheduled jobs get pinned scopes at registration — cracks-brief is the precedent (cross-estate read, owner-direct notify-only sink).
   - **Writes.** Single-estate sessions write their own estate. `shared` is small and curated — writing to it IS declassification: explicit owner promotion or audited high-confidence classifier only (identity/calendar/travel-class facts). Cross-estate sessions do NOT auto-write-back in v1 (read-mostly; persisting requires the owner to pick an estate). Every write carries mandatory provenance: (source, source_trust, estate, sensitivity) — this is the day-one invariant that keeps the model tightenable later.
   - **Confidential tier.** `work_confidential` (NDA'd/client material): in scope only for owner + work surface + strong auth; NEVER in cross-estate scope; cognify and recall for it pinned to approved LLM providers (never OpenRouter free tier — cognify ships every chunk to the LLM); prefer pointer-not-ingest for the worst documents.
   - **Output/actuation = layer 2, not layer 1.** Sink labels (cross-estate context → owner-direct sinks only), trust-engine gating, law 1 draft-never-send. Provider routing is an output boundary too: work/confidential context never goes to free/logging providers, at cognify OR recall time.
   - Reuse the estate-resolution from cracks-brief; fix the upstream plaud estate mislabel BEFORE backfill — under dataset walls a misfiled estate is a cross-wall leak, not a cosmetic bug.

## The one API
A single memory API (extend intake `/v1/memory/*` or a `mimir-memory` service) that every capability + Odin calls: `remember(text, estate, tags, provenance)` / `recall(query, session_ctx, k)` → cognee. The API resolves recall scope from `session_ctx` via the ingress registry (§4 above) — callers never pass a raw namespace; a caller-chosen namespace is exactly the spoofable surface §4 forbids. Capabilities STOP talking to `memory.entries` / brain.db directly. This API is the coupling point — it's what makes cracks-brief, fab, graphify-context, and Odin share one memory.

## Migrate the consumers (after cutover)
Point live consumers at the one API: Odin `memory_save`/`memory_search`, intake memory endpoints, ZeroClaw recall (Hermes). brain.db + `search.embeddings` become read-only, then retired.

## Acceptance
- Backfill complete; parity verified (cognee recall ≥ old on a held-out set spanning both estates).
- Shadow-write soak clean N days; namespaces enforced (no cross-estate leak in a probe).
- One API serves all consumers; pgvector floor still answerable.
- No production memory lost (old stores retained cold).

## Fable review gate (advisory, not blocking)
Because this is the foundational layer, the migration PLAN (the staging above) is worth a Fable review before the **cutover** step (e). The pgvector floor + no-drop staging make it recoverable, so it's advisory — but this is a legitimate break-glass moment if any step feels ambiguous.

## Executor decides / verify first
cognee prod deploy specifics (db-host host, the 422 fix), backfill mechanics, shadow-write plumbing, the parity query set, cutover orchestration. Read the bake-off artifacts before starting. Do NOT skip the shadow-write soak to save time — the soak is the safety.
