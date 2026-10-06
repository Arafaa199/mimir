# Memory-spine bake-off (spec 03, phase 1)

Measured **cognee (graph+vector)** vs **pgvector-hybrid (incumbent)** on 7 dense work
meeting docs, 16 real queries (4 classes). Read-only — **nothing migrated.**

## Result → **DECISION: adopt cognee as the one spine**
cognee's graph beat the incumbent **+71% on the relational+temporal band** (the queries Jarvis
lives on), and a fairness control (A's chunks → the same LLM = "A+RAG") proved the **graph, not
the LLM, is the differentiator** (cognee-GRAPH beats A+RAG +300%). Full numbers + reasoning in
`report.md` (private; the headline numbers are in the top-level README). Verdict taken by the owner (2026-07-09): **cognee becomes the spine** (holds
vectors + graph in one store), pgvector kept as the recall floor, cognify funded (~$1/run;
→ $0 with a future GPU node).

## What's here
Not in this public copy: `report.md`, `results_*.json`, `corpus_final.txt` and
`queries_draft.json`. They hold the private corpus, the queries and the answers drawn
from it.

- `bakeoff_lib.py` `schema_a.sql` — contender A (faithful copy of prod `search.hybrid_search`).
- `cognify_corpus.py` `retrieve_b.py` `with_env.sh` — contender B (cognee) driver + config.
- `a_rag.py` `retrieve_a.py` `judge.py` `report.py` — harness (RAG baseline, retrieval, judge, agg).
- `prov.sh` — provisions the isolated `bakeoff` role + `mimir_bakeoff`/`cognee_bakeoff` DBs on db-host.

## Working config (what actually ran)
- LLM = **OpenRouter paid `google/gemini-2.5-flash`** (`LLM_MAX_COMPLETION_TOKENS=8192`).
- Embeddings = **fastembed `bge-small` 384** (cognee↔ollama-nomic threw 422s; phase-2 = fix nomic path).
- Store = db-host pgvector (isolated DBs) + embedded kuzu graph.
- **`bakeoff.env` (creds) is intentionally NOT committed** — re-provision with `prov.sh` + regenerate it.

## To re-run / scale-confirm
Recreate `venv` (`uv venv --python 3.12`, `uv pip install -e '<local cognee checkout, pinned to upstream>[postgres-binary]' fastembed transformers psycopg2-binary`),
regenerate `bakeoff.env`, then `./with_env.sh ./venv/bin/python cognify_corpus.py` etc. Scale by
editing `corpus_final.txt` (20–36 docs) — OpenRouter is funded now.

## Next: phase 2 (design + build)
Migrate onto cognee: nomic-768 (one embedding space), estate namespaces (personal/work/shared),
one read/write API every surface uses, retire the separate indexes. Not built yet.
