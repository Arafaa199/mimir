# mimir-memory — the one recall API (spec 07)

**LIVE on db-host tailnet `localhost:8410`** (`systemctl --user status mimir-memory`).
This is the bridge in "walls at storage, bridge in the API" (spec 05 §4).

## Use it

```bash
source ~/.config/mimir-memory.env      # per-ingress keys live here (chmod 600)

curl -s http://localhost:8410/v1/recall \
  -H "Content-Type: application/json" -H "X-Ingress-Key: $MIMIR_KEY_CLI" \
  -d '{"ingress":"cli","query":"what runs on db-host?","k":3}'
```

You name your **ingress**, never an estate. The registry decides what that ingress may see.

## Why the request shape IS the security model

A caller cannot:
- **pick its own estate/namespace** — it presents a registered ingress + its key, and
  `ingress.py` says what that surface may see. A caller-chosen namespace is exactly the
  spoofable surface §4 forbids;
- **widen scope** — `datasets` only ever INTERSECTS what the ingress holds (`scope._narrow`).
  Widening isn't rejected, it's *unrepresentable*;
- **escalate from an untrusted surface** — `odin_group` / `email_inbound` have
  `may_escalate=False`. A hostile Telegram message containing `/bothestates` is parsed as a
  verb by the router **and still buys nothing**;
- **reach `work_confidential` content** — ever, from anywhere. It returns POINTERS only
  ("retrieve the source directly"), from the ledger, never the graph. Its body has never
  reached any LLM and recall is not the hole that changes that.

Escalation to cross-estate = owner-strong ingress + a verb parsed by **deterministic router
code** (`parse_escalation_verb`), never by a model. A model that can talk itself into
another estate is a model an injected message can talk into another estate.

## No LLM at read time

`cognee.search(only_context=True)` returns graph facts and passages **without** a completion.
So recall is **local** (query embedded on db-host ollama), costs **$0**, and sends work /
confidential context to **no provider** — closing §4's "the provider is a sink" hole at
recall time, not just at cognify. Retrieval here; judgment in the caller (design law 6).

## Verified (2026-07-13, live)

| probe | result |
|---|---|
| owner CLI → personal recall | ✅ real graph content returned |
| `work_confidential` → any surface | ✅ pointers only, content withheld |
| Telegram group asks for `work` | ✅ **403** |
| Telegram group sends `/bothestates` + `escalate` | ✅ **403** — untrusted may never escalate |
| forged ingress, no key | ✅ **401** |
| unregistered ingress | ✅ **403** — fail closed |
| every request, allowed **and denied** | ✅ in `ops.action_audit` (query hashed, never stored) |

Attack tests: 26 in `test_scope.py`, 7 in `test_write.py`. They need no database:
`PYTHONPATH=. ./venv/bin/python -m pytest test_scope.py test_write.py`

## The floor found a real leak (2026-07-13)

Wiring the floor surfaced a live estate leak — in the CLASSIFIER, not in the API:

`WORK_KEY_RX` was `^\s*\[?\s*Work/` — it required a literal slash after the name, so
ZeroClaw's `[WorkMemory/ws-*.md :: ...]` headers sailed past it. And `estate_for_memory`
trusted the namespace: for a personal namespace, anything not matching that regex was
personal. **Measured: of 7,542 rows opening with a Work header, 281 were filed PERSONAL**
(266 via the regex hole; 15 whose Work-ness is in prose, e.g. `[Work circuit-breaker
stuck for 36+ hours]`). A personal-session recall really did return a work data-processing agreement.

Fixed at the source (`estates.py`, used by BOTH spine and floor):
- regex `Work\w*/` — catches `WorkMemory/`, `WorkBulk/`;
- **content beats namespace**: a personal namespace is not a promise about the body, so an
  unmistakably-work body (`is_work_content`) is work whoever filed it.
Combined, the guards catch **281/281**. Verified: no work content reaches a personal
session, and a gym note still classifies personal (no over-correction).

**The spine was NOT contaminated (0 units)** — purely because the 281 sit in the
*un-cognified* personal remainder and personal backfill was deferred. It would have baked
into the graph the moment personal resumed.

### ⛔ BLOCKING before personal backfill resumes
`corpus.jsonl` still holds estate labels computed by the OLD classifier, and `backfill.py`
routes on those precomputed labels — so the 281 would still land in `personal`.
**Re-run `extract_corpus.py`** (which resolves the estate ONCE per document and inherits it
to sections) and diff against the ledger before resuming. Do NOT re-label corpus.jsonl
per-unit: section provenance (`vault:x.md#4:Heading`) and section text lack the document's
header, so a naive per-unit pass re-introduces the exact bug it is trying to fix (tried;
it moved 2,670 units and was reverted from
`.state/corpus.jsonl.bak-20260713-preestatefix`).

## Known constraints

- **kuzu locks the estate being cognified.** The graph DB is an embedded file with an
  exclusive lock, so recall on an estate currently being backfilled fails. ACL-ON gives each
  estate its OWN graph file, so the other estates keep serving — but a nightly shadow-write
  will briefly lock whatever it touches. If "always answerable" becomes a hard requirement,
  the fix is a server-backed graph (neo4j), not a workaround here.
- **Coverage = what is backfilled.** Today: personal 575, work 354/1,954 (running),
  confidential 1,977 (pointers). **pgvector remains the recall FLOOR** and is NOT yet wired
  in — `floor.py` is the intended seam. Until it is, recall answers only from the spine.
- Keys are per-ingress in `~/.config/mimir-memory.env`. An ingress with no key configured
  cannot be used at all (fail closed, not fail open).

## MCP server — the brain as tools in Claude Code

    claude mcp add mimir --scope user -- python3 <repo>/jobs/memory-api/mcp_server.py

Tools: `memory_recall`, `memory_remember`, `memory_scope`. Zero deps (stdlib only).

**The model must never choose its own scope.** A tool PARAMETER is by construction chosen by
the model — so this server exposes none that can widen access:
- no `escalate` arg → cross-estate is `MIMIR_MCP_CROSS_ESTATE=true`, an env var YOU set when
  you launch the session. A deliberate human act, outside the model's reach. This is exactly
  §4's "scope is fixed at session creation".
- no `estate`/`datasets` arg → the session's estate is `MIMIR_MCP_INGRESS` (default
  `claude_code` = personal+shared; set `work_cli` in a work repo).
- no `declassify` arg → writing to `shared` stays something you do yourself.

`memory_scope` exists so the model can tell "the brain doesn't know" from "this session may
not see that estate" — two very different things it must never guess between.

## Known: recall degrades while the backfill runs

The spine's query embedding and the backfill's cognify both hit **the same ollama on db-host's
4 cores**. Under backfill load the query embed times out and recall answers from the FLOOR
alone (reported honestly in `health`). Verified: with the backfill paused, spine recall
returns real GRAPH NODES (entities + descriptions), not just chunks. So this is contention,
not breakage — and it ends when the backfill does.

`api.py` MUST call `nomic_engine.install()` before anything imports cognee, or every spine
recall dies with `EmbeddingException: Failed to index data` (cognee's stock Ollama path has
the hardcoded-60s-timeout defect that spec 05 stage 0 root-caused).

## Next
1. **Odin wiring** — point Telegram/WhatsApp/glasses at `:8410` so the brain is on every
   surface, not just the terminal. This is what makes it ubiquitous.
2. Schedule `drain.py` (a timer) so writes reach the graph without a manual run.
3. Migrate consumers off `memory.entries` / brain.db (spec 05, post-cutover).
