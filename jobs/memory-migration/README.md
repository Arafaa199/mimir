# Memory-spine migration (spec 05, phase 2)

Collapse 4 stores / 3 embedding spaces onto **one cognee spine**. Staged and
reversible: stand up → backfill → shadow-write → verify parity → **STOP for
go/no-go** → cutover → retire (never delete). Old stores stay cold.

**Nothing is migrated yet. No production memory has been moved, and none can be
lost: every store is read-only here, and the old ones stay authoritative until a
cutover that has not happened.**

| stage | state |
|---|---|
| 0 · embedding path (the "422") | **DONE** — root-caused, fixed, verified |
| A · cognee prod on db-host | **DONE** — stood up; estate leak found and closed |
| B · backfill | **RUNNING on db-host** — restarted 2026-07-09 under corrected estates |
| C · shadow-write soak | **ARMED** — `mimir-shadow-tail.timer`, daily 04:00 UTC; skips while backfill runs |
| D · parity | harness written (bar fixed in advance); **needs the owner-verified queries** |
| E · cutover | **blocked on the owner's go/no-go** — as specified |

## Stage 0 — the cognee↔ollama-nomic "422", root-caused and fixed

The bake-off blamed "huge/empty texts". That was wrong. Measured, not inferred
(`repro_422.py` reproduces; `verify_422_fix.py` proves the fix):

**The 422 is not an HTTP status from ollama.** It is the default `status_code` of
cognee's own `EmbeddingException` (a `CogneeConfigurationError`). Every observed
failure is a masked `asyncio.TimeoutError`. Three compounding bugs in
`OllamaEmbeddingEngine`:

1. **Unbounded fan-out** — `embed_text()` gathers the entire batch at once, and
   cognee's default embedding batch size is **36** (`embeddings/config.py:107`).
   Ollama serialises per model, so the tail of every batch queues behind the head.
2. **Hardcoded 60 s timeout** — `_get_embedding()` passes `timeout=60.0` to
   aiohttp with no override. Under (1) the tail always blows it.
3. **The rescue path is blind to it** — `embed_text()` only reacts to substrings
   like `"context length"` in `str(error)`, and `str(TimeoutError())` is the
   **empty string**. The reactive split never fires; the timeout is rethrown as
   `EmbeddingException(..., status_code=422)`.

Amplifier: no client-side truncation. Ollama truncates to the model's context
window (nomic `n_ctx=2048`) but only *after* tokenising the whole payload — a
200 KB data point costs ~23 s of pure tokenisation on a CPU-only host.

Empty/whitespace/`None` inputs are **not** the cause: `sanitize_embedding_text_inputs`
already maps them to a dummy and `handle_embedding_response` zeroes them.

### The fix — `nomic_engine.py`
`NomicOllamaEmbeddingEngine` subclasses the upstream engine and adds: bounded
concurrency (semaphore, default 4), client-side truncation (12 000 chars ≥ 2048
tokens for any text down to 5.8 chars/token), a configurable timeout (default
180 s), retry with backoff, and a guard on ollama's `{"embeddings": []}` reply
(upstream indexes `[0]` straight into an `IndexError`).

It **fails loud**. An infra failure must never degrade to a zero vector: that is a
silent, permanent hole in recall that is indistinguishable from a successful write.
Only genuinely non-embeddable inputs get zeroed, upstream, by cognee itself.

`install()` patches the **factory**, not the class — the local cognee checkout
(pinned to upstream) stays byte-identical. Call it before any
`cognee.add`/`cognify`.

### Result
| | before | after |
|---|---|---|
| 11 adversarial cases, healthy host | 4 FAIL (130 s each) | **11 PASS, 9.6 s total** |
| `batch-36` (cognee's real default) | timeout | 0.2 s |
| 526 KB single data point | timeout | 0.1 s |

```
./venv/bin/python repro_422.py                          # reproduce (unpatched engine)
./venv/bin/python verify_422_fix.py                     # prove the fix
OLLAMA_EMBED_ENDPOINT=http://localhost:11434/api/embed ./venv/bin/python verify_422_fix.py
```

Env knobs: `NOMIC_EMBED_CONCURRENCY` `NOMIC_EMBED_TIMEOUT` `NOMIC_EMBED_RETRIES`
`NOMIC_MAX_INPUT_CHARS`.

## Stage A — cognee prod on db-host, and the estate leak

`prov_prod.sh` (idempotent) creates role `cognee` + DB `cognee_prod` + pgvector on
the db-host Postgres 16 instance. Strictly additive. It **asserts** its blast radius
rather than assuming it: the role has no SELECT/INSERT/UPDATE/DELETE on
`memory.entries`, no rights on `search.embeddings`, and no USAGE on schema
`memory`. The script fails closed if any of those is true.

It does **not** revoke `CONNECT` from `PUBLIC` on the prod DB — that grant is
shared with every other prod consumer, so removing it to tighten one role would
have broken them.

### The estate leak (found by the Stage A smoke, fixed by config)

Spec 05 §4 makes estate integrity load-bearing: *work must never leak into
personal recall.* The bake-off ran `ENABLE_BACKEND_ACCESS_CONTROL=False`. Under
that setting **cognee's `datasets=[...]` argument is silently ignored**:
`search.py` takes the `else` branch and "runs search without setting database
context", querying one global vector+graph store. Asking the `personal` dataset
*"Who owns the scheduling workstream?"* returned a work colleague's name.

Post-filtering cannot fix this: by the time a result is returned, the LLM has
already synthesised its answer from the leaked nodes.

`ENABLE_BACKEND_ACCESS_CONTROL=True` gives every dataset **its own vector and
graph database** and runs each search in that database's context. pgvector and
kuzu are both on cognee's multi-user support lists. Matched-pair evidence from
`probe_estate_leak.py`:

| retriever | ACL=False (bake-off) | ACL=True (prod) |
|---|---|---|
| `GRAPH_COMPLETION` personal ← work question | **LEAK (colleague name)** | clean |
| `GRAPH_COMPLETION` work ← personal question | **LEAK `ender`** | clean |
| `CHUNKS`, `SUMMARIES` (both directions) | leak / global store | clean |

Two consequences, both accepted:
1. The role needs `CREATEDB`, and pgvector is **not** a trusted extension in this
   image — so `vector` is installed into `template1` once, as superuser, and every
   per-dataset DB inherits it. Additive; no existing database is altered.
2. **A graph answer can never span estates.** `recall(q, "personal")` queries
   `[personal, shared]` and returns one answer envelope per dataset; there is no
   single graph that reasons across work and personal. That is the price of
   §4's hard isolation, and it is the spec's own stated priority. Deliberately
   cross-estate facts go in the `shared` dataset.

Stage A acceptance (`smoke_prod.py`, live against `cognee_prod`): patched nomic-768
engine → cognify (21 s, 2 docs) → pgvector chunks + kuzu graph → **4/4 PASS**,
including estate isolation.

## What the production survey found (blocks stages B/C — see the report)

Profiling the three stores before backfill surfaced a live defect that sits
directly on this migration's critical path.

**~98% of production "memory" is duplicate rows.**

| store | rows | distinct contents | waste |
|---|---:|---:|---:|
| `memory.entries` (db-host pg, nomic-768) | 306 725 | **4 089** | 98.7% |
| `search.embeddings` `agent_memory` (db-host pg, MiniLM-384) | 284 351 | **4 438** | 98.4% |
| `brain.db` `memories` (worker sqlite, nomic-768 f32) | 308 185 | **10 867** | 96.5% |

Cause: `seed-memory.py` (`created_by='doc_seeder'`) re-sends its ~24 handcrafted
entries on **every** run, via `POST /v1/memory/batch-save` — the one memory
endpoint with **no dedup** (`/v1/memory/save` has a 0.8-similarity check;
`handle_memory_batch_save` does not). `memory.save_with_embedding()` is a blind
`INSERT ... gen_random_uuid()`. It is triggered by the `com.example.internal.docs-assembler`
fswatch agent, and `memory.run_hygiene()` explicitly *counts* duplicate `profile`
rows but never deletes them. **7 380 duplicate rows landed in the last 24 h.**

Second-order: the Synapse embedding worker re-embeds every duplicate row with
MiniLM, so it runs ~29 min out of every 30 and pins worker (load ~17, 698% CPU).
**That saturation is what makes the nomic embed path time out at all** — the same
adversarial batch that passes in 0.8 s on an idle host takes 294 s on worker.

Union of distinct memory across `memory.entries` + `brain.db`: **11 159 items**
(3 821 shared, 7 058 brain-only — incl. 6 513 `work`/work, 280 memory-only).

Consequence for this migration: a naive shadow-write would re-cognify the same
handcrafted entries thousands of times a day, at LLM cost per write.

## What the pilot corrected (running it beat estimating it)

**The bake-off's cost model was wrong by ~12x.** It reported `$0.67/MB`. Metering
`litellm.acompletion` directly (`cost_meter.py`) shows cognee's ECL makes **~1.3 LLM
calls per ~1.2 KB chunk** — cost tracks *chunks*, not bytes and not documents. Two
probes settle it: 40 one-chunk docs → 1.7 calls/doc; 6 twelve-chunk docs → **15.8
calls/doc**. The 25 811-chunk corpus is therefore **~$164 on gemini-2.5-flash**, not
$15. On **gemini-2.5-flash-lite** ($0.10/$0.40 per M vs $0.30/$2.50, and this token
mix is completion-dominated) the same corpus is **~$29**. `validate_model.py` re-runs
the bake-off's own 7 docs / 16 queries on the candidate model and refuses it unless
it keeps ≥90% of flash's relational+temporal score *and* still beats the incumbent
by ≥30%.

### The judge was the biggest free variable

`validate_model.py` first said flash-lite **FAILED** (rel+temp 0.75 vs a "baseline"
of 1.50). That was an artifact: the bake-off scored with **Claude**, my harness
scored with **gemini-2.5-flash**. Judging the *same* cognee answers with the two
judges gives **1.69 vs 0.94** overall. The harness now re-judges the bake-off's saved
answers (`results_A.json`, `results_B.json`) with the very same judge it uses on the
candidate — every number below comes from one judge.

The judge is also not deterministic at `temperature=0` (OpenRouter routes across
providers): re-scoring one fixed answer set gave rel+temp `0.75 / 0.75 / 0.75 / 0.88`
— a spread of exactly one flipped query (0.125). A single-shot score cannot resolve a
10% gate, so each item is judged 3× and the median taken, and the gate allows a
one-flip deficit. **The gate must not be tighter than the instrument.**

**All four systems, one judge (median of 3), n=16:**

| class | incumbent pgvector | cognee · flash | cognee · flash-lite |
|---|---:|---:|---:|
| factual | 0.75 | 1.25 | 1.25 |
| relational | 0.25 | 0.75 | 0.50 |
| temporal | 0.75 | 0.75 | 1.00 |
| cross-source | 0.50 | 1.00 | 0.75 |
| **DISCRIM rel+temp** | **0.50** | **0.75** | **0.75** |
| ALL | 0.56 | 0.94 | 0.88 |

- **flash-lite ties flash on the discriminating band (+0%)** and both beat the
  incumbent by **+50%** there. VERDICT: **PASS** — the ~$29 path is real.
- flash-lite is modestly worse overall (0.88 vs 0.94), trading relational and
  cross-source for temporal. At n=16 that gap is ~1 query.
- **The spec-03 decision survives, at a smaller magnitude.** The bake-off's headline
  "+71%" was Claude-judged; under one consistent judge the same win is **+50%**. Still
  far past the pre-committed ≥30% bar, so adopting cognee remains correct — but the
  headline number in `report.md` should be read as judge-dependent.

There is **no free path at this scale**: Gemini's free tier is 20 requests/day/model
(~15 chunks/day), worker's qwen2.5:7b is CPU-only at >2 min/call, and OpenRouter's
free pool 429s under load. Free models are for probes, not for 25 811 chunks.

**A poison document can kill a batch.** Gemini's content filter rejects an HTB
exploit write-up outright (`ContentPolicyFilterError`). `backfill.py` now isolates a
failed batch per document and writes the offender to `.state/quarantine.jsonl` with
its reason. It is never silently skipped — that would delete a memory the owner
still holds in the old store, and nobody would know which one.

**The estate rule had a real leak in it.** "Under the Work vault path" was coded
as a prefix match, so `Claude/Memory/Work/ws-*.md` — 171 work workstream notes
— were labelled *personal*. Fixed to match a path **component**. And the keyword
matcher was substring-based, so a short work keyword fired inside ordinary words (in
the example config, `rota` inside `rotation` and `rotate`), mislabelling 262 personal
documents. Both found by reading the contents
of a failing batch, not by any test.

## Estates (`estates.py`)

Work must never leak into personal recall (§4). A work→personal mislabel *is* that
leak; personal→work is merely misfiled. Every ambiguity resolves to **work**.

The term lists (employer name, strong and weak work terms, NDA client terms) are
deployment data. They load from `$MIMIR_ESTATES_FILE`, else `estates.json` beside the
module, else the shipped `estates.example.json`, whose values are invented.

Path rules alone are insufficient — a Daily journal mixes a gym session and a client
meeting in one file. So a document whose *path* says personal but whose *body*
carries work signal is **split by markdown section**, and each section is labelled
independently. Only mixed documents are split (1 175 of them); everything else stays
whole, because cognee builds a better graph from a whole document than from
fragments.

Three bugs found here, one a live §4 leak:

1. **The leak.** ZeroClaw pushes Work vault chunks into `memory.entries`; their
   bodies open with a `[Work/...]` header. Section splitting strips that header from
   sections 1..N, so a per-section provenance test dropped ~1 580 genuinely-work
   fragments (ops runbook rotations, ELB target groups, client report names) into
   **personal** recall. Provenance is now resolved **once, on the whole document**, and inherited by
   every section; a provenance-work document is never split.
2. **`\b` treats `-` as a word boundary**, so a weak work term matched inside a
   hardware model name (a motherboard SKU ending in `-<TERM>`; in the example config,
   `\brota\b` would match `XR80-ROTA`). Hyphenated alphanumeric identifiers are
   scrubbed on both sides before matching (`<Employer>-Claude-MCP` is an Azure
   resource, not a mention). Its mirror image — substring matching, where the term hid
   inside ordinary words — was fixed earlier by word boundaries.
3. **The employer's name** appears as a label in personal infra notes. Treating one mention as decisive costs ~2 000 units of homelab documentation
   their place in personal recall. **The owner's call: it stays STRONG** — maximum
   leak-aversion, per the "ambiguous → work" tie-break. `ESTATE_WORK_WEAK=true` opts
   out.

| | units | MB | chunks | ~$ (flash-lite) |
|---|---:|---:|---:|---:|
| personal | 5 951 | 4.30 | 7 895 | ~$9 |
| work | 3 731 | 17.19 | 17 098 | ~$19 |
| **total** | **9 682** | **21.49** | **24 993** | **~$28** |

The 575 units already cognified under the old rules were **pruned, not reused**: 14
were fragments whose parent document is now work, and `memory:zeroclaw/agent_observation`
is not a unique document id, so their safety could not be *proven*. $0.66 sunk beats an
unprovable estate boundary.

## Embedding host — vectors are NOT host-independent

An earlier version of this file claimed they were, because the model digest matches
(`0a109f422b47`) on every host. **That was wrong, and it would have silently poisoned
the whole spine.**

| pair | cosine |
|---|---:|
| laptop ↔ laptop (repeat) | 1.00000000 |
| worker ↔ db-host | 1.00000000 |
| **laptop ↔ worker** | **0.82607839** |
| a stored `memory.entries` vector, re-embedded on **worker** | **1.000000** |
| the same row, re-embedded on **laptop** | **0.868693** |

laptop runs `ollama 0.31.1`, worker runs `0.16.1`; the newer build pools
nomic-embed-text differently. Production (`intake/handlers/memory.py`,
`OLLAMA_URL=localhost`) embeds on **worker**, so *worker's space is the
production space*. Backfilling from laptop would have built a store whose vectors sit
0.87 from every query the production API later issues — a corpus-wide retrieval
regression invisible to any single-host test, because within one run everything is
self-consistent.

`check_embed_space.py` re-embeds real stored `memory.entries` rows and refuses to
proceed below cosine 0.999. `with_env_prod.sh` now defaults `OLLAMA_EMBED_HOST` to
worker. db-host's ollama (LAN-bound on `localhost:11434`, and it *is* listening — the
earlier "nothing listening" note was a localhost-vs-LAN-bind mistake) reproduces
worker's space exactly but is CPU-bound on 4 cores (~13 s for 2 KB), so worker stays
the embedding host.

## Running the backfill (on db-host)

db-host is the host: the job is ~2 days of wall-clock and laptop roams off the home LAN.
Code + venv live in `~/mimir-memory` on db-host; secrets are a scoped, chmod-600
`~/.config/mimir-memory.env` holding only `OPENROUTER_API_KEY` and `COGNEE_PGPASSWORD`.

```
ssh db-host 'cd ~/mimir-memory && ./run_on_db_host.sh check'    # preconditions, writes nothing
ssh db-host 'cd ~/mimir-memory && ./run_on_db_host.sh full'     # resumable, survives logout
ssh db-host 'cd ~/mimir-memory && ./run_on_db_host.sh status'   # progress + spend
```

Three hard preconditions, all fail-closed:
1. `ENABLE_BACKEND_ACCESS_CONTROL=True`, else the estates share one store.
2. `check_embed_space.py` — the endpoint must reproduce production's embedding space
   (cosine ≥ 0.999 against real stored `memory.entries` vectors).
3. `--max-usd` (default $45) — metered spend, checked between batches where the
   checkpoint is consistent, so stopping is free and resuming re-does nothing.

Measured on the live run: ~4.5 LLM calls/unit, **$0.0023/unit** ⇒ ~$25 for 10 817
pending units. `.state/backfilled.jsonl` is an append-only fsync'd checkpoint.

Known cruft: a `flashlite_probe` dataset from `validate_model.py` sits in its own
database, embedded in **laptop's** space. It is never queried by any recall path, but
do not treat it as spine content.

## Stage C — shadow-write (`shadow_tail.py`)

A **watermark tailer**, not a hook in the write path.

There are at least nine writers across the three stores (intake `/v1/memory/save` and
`/batch-save`, `seed-memory.py`, `memory-synthesis.py`, `zeroclaw-memory-sync` push
*and* pull, ZeroClaw's own `memory_store` tool, `sync-work`, `zeroclaw-event-sync`,
plus direct SQL). Hooking each is nine chances to miss one — and a missed writer is a
silent hole in the spine that only surfaces at cutover. A tailer reads *what actually
landed*: it cannot miss a writer it has never heard of, and it touches no production
write path, so it cannot break one.

It is also the only shape consistent with design law 2 (reasoning is one batched pass
per day) and with cognee's economics (LLM extraction per chunk). Per-write cognify
would re-pay for the same content on every `doc_seeder` touch.

Idempotent by construction: cognee's `incremental_loading` keys on a content hash, so
re-presenting text it already processed is a cheap lookup, not another LLM call. The
watermark is an optimisation, not a correctness requirement — overlap is free, and it
advances **only after** `cognify()` returns.

- Each source keeps its watermark in **its own timestamp dialect**: Postgres
  `timestamptz` for `memory.entries` / `raw.notes_index`, and a **naive** ISO string
  for `brain.db`, whose `updated_at` is TEXT compared lexicographically by sqlite.
  Feeding it an offset-suffixed value would make the comparison nonsense.
- Mixed documents are section-split and re-labelled exactly as in the backfill, so a
  Daily note written during the soak cannot put its client section into personal recall.
- A shared `flock` (`.state/spine.lock`) plus a `pgrep` guard keeps the tailer and the
  backfill off the same dataset — `cognify()` operates on a whole dataset, not an item.
- Fails loud: an unreachable source aborts the pass and advances **no** watermark.

```
ssh db-host 'cd ~/mimir-memory && ./run_on_db_host.sh shadow --dry-run'   # reads only
systemctl --user status mimir-shadow-tail.timer                        # db-host
```

Soak metrics accumulate in `.state/soak.jsonl` (units, per-estate counts, spend,
quarantines, watermarks) — that is the evidence for the soak-clean claim.

## Layout
- `nomic_engine.py` — the fixed cognee↔ollama-nomic embedding engine + `install()`.
- `repro_422.py` / `verify_422_fix.py` — the root-cause repro and its proof.
- `prov_prod.sh` — idempotent, additive provisioning of `cognee_prod` on db-host.
- `with_env_prod.sh` — prod cognee config; secrets stay in-process.
- `smoke_prod.py` — Stage A acceptance, incl. the estate-isolation assertion.
- `probe_estate_leak.py` — the ACL=False vs ACL=True leak evidence.
- `estates.py` — path + content estate rules, section splitting for mixed docs.
- `extract_corpus.py` — read-only corpus build (`.state/corpus.jsonl`).
- `backfill.py` — resumable, spend-capped, quarantining backfill.
- `cost_meter.py` — wraps `litellm.acompletion`; the real $/chunk.
- `check_embed_space.py` — refuses a non-production embedding space.
- `validate_model.py` — one-judge model gate (flash-lite vs flash vs incumbent).
- `parity.py` — stage-D go/no-go harness (needs a held-out query set).
- `run_on_db_host.sh` — the db-host runner.

Credential: `COGNEE_PGPASSWORD` in `~/.config/claude-mcp-secrets.env` (chmod 600).
