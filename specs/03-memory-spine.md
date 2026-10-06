# Spec 03 — Memory Spine (the convergence · phase 1: bake-off)

**STATUS: ready for executor (bake-off phase only).** This is the foundational convergence step — collapsing 4 memory stores / 3 embedding spaces into one. **Phase 1 is a MEASURED bake-off; migrate nothing.** The migration (phase 2) is designed only after the result + a Fable review.

## Why
Today: 4 memory stores (brain.db, `memory.entries`, `search.embeddings`, CC memory), 3 embedding spaces (384-MiniLM personal-only / 768 `memory.entries` / 768 brain.db), no cross-estate retrieval. Every capability so far reads its own data. "One brain, one memory" (the locked ubiquity-first priority) needs ONE spine every surface reads/writes. Before committing to a store, measure — adding a store for a tie makes the sprawl worse.

## Phase 1: the bake-off (build THIS)
Decide the spine by measurement, not vibes.

**Contenders:**
- **A — pgvector-hybrid (incumbent):** existing `search.hybrid_search` / `memory.hybrid_search` (40% text / 60% vector, 768-dim nomic). Already built; zero new dependency.
- **B — cognee (challenger):** ECL (extract entities+relations → kuzu graph + pgvector vectors → graph+vector query). Runs from a local cognee checkout pinned to upstream; can use db-host pgvector as its vector backend.
- Not testing a third — keep it bounded. If BOTH underperform on relational queries, say so: that points to a lightweight typed-facts table as a cheaper phase-2 option.

**Corpus:** the **work vault** — richest relational/temporal structure (people, projects, meetings, commitments, deadlines). The discriminating test: if a graph can't beat vectors on relationship-heavy data, it won't justify itself on flatter personal notes. Snapshot it so both contenders see identical input; reuse the brain.db chunking if present.

**Query set:** 15–25 REAL queries the owner cares about, four classes (The owner confirms/fills specifics — do NOT fabricate project/people names):
- Factual ("what is <person>'s role") — vectors should handle these.
- Relational ("who's involved in <project> / what's blocked by <Y>") — graph should win.
- Temporal ("what changed about <X> since <date> / what did I commit to on <topic>") — graph/temporal should win.
- Cross-source ("status of <project> across notes + tasks + meetings").
The discriminating classes are **relational + temporal** — where Jarvis needs strength and where a graph earns its cost.

**Metrics (pre-registered, so the result is honest):**
- Relevance: for each query, does top-k (k=5) contain the correct answer? Score 0/1/2 (miss/partial/hit); the owner or an LLM-judge-with-ground-truth scores. Report mean by query class.
- Cost: cognify token spend + ingestion time for the corpus; whether updates need re-cognify (maintenance burden). Run cognify on **local qwen (worker swap) or free-tier OpenRouter** — keep it near-zero.
- Operability: lines of glue, new services/deps, failure modes.

**Decision rule (pre-committed):** adopt cognee as the spine ONLY if it beats pgvector-hybrid by a meaningful margin on the RELATIONAL+TEMPORAL classes (≈≥30% higher mean relevance there) AND cognify cost/maintenance is acceptable on the free/local path. Wash, or wins only on factual → keep pgvector-hybrid (incumbent wins ties; no dependency for a tie), and note whether a small typed-facts table would close the relational gap more cheaply.

**Deliverable:** a one-page scored comparison (relevance by class · cost · operability) + a recommendation. **This is the Fable moment** — interpreting the result and committing to the spine is the expensive-and-ambiguous call; take it to the Fable window.

## Phase 2 (DESIGN LATER — gated on result + Fable review)
The spine migration: unify onto the winner, one embedding space, estate namespaces (personal/work/shared — `memory.entries` already has `namespace` + `visibility`), one read/write API every surface + capability uses, retire the losers as separate indexes. Do NOT build until phase 1 decides.

## Host / inference
Run on db-host (pgvector local); cognify LLM → worker-ollama (qwen2.5:7b) or free OpenRouter; embeddings nomic-768 (already in use). Read-only against a corpus snapshot — no production writes.

## Acceptance (phase 1)
A scored comparison exists, the decision rule is applied, and there's a clear recommendation (adopt cognee / keep pgvector / keep pgvector + typed-facts). No store migrated. The owner + Fable review before phase 2.

## Executor decides
cognee install/config + backend wiring, the ground-truth scoring harness, corpus-snapshot mechanism, cognify model choice (local vs free). Reuse existing pgvector/hybrid_search for contender A.

## Verify first
- Work vault snapshot path + size; whether brain.db already has it chunked (reuse).
- cognee runs with a pgvector backend + local/free LLM (no paid API).
- Get the owner's 15–25 real queries + ground-truth answers before scoring.
