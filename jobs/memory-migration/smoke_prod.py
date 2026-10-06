#!/usr/bin/env python
"""Stage A acceptance: cognee prod on db-host, end-to-end, on nomic-768.

Proves the whole prod path in one run: the patched embedding engine -> cognify
(OpenRouter gemini-2.5-flash) -> pgvector on db-host `cognee_prod` + kuzu graph ->
GRAPH_COMPLETION search. Tiny corpus (2 docs, ~$0.01) — this validates plumbing,
not relevance; relevance was settled by the phase-1 bake-off.

Also asserts estate isolation at the dataset boundary: a `work` question must not
be answerable from the `personal` dataset.

  ./with_env_prod.sh ./venv/bin/python smoke_prod.py
"""
import asyncio
import os
import sys
import time

import nomic_engine

nomic_engine.install()

import cognee  # noqa: E402
from cognee.infrastructure.databases.vector.embeddings import get_embedding_engine  # noqa: E402
from cognee.modules.search.types import SearchType  # noqa: E402

PERSONAL = "smoke_personal"
WORK = "smoke_work"

PERSONAL_DOC = """# Homelab note
The db-host host runs the Postgres 16 instance and the mimir-fab service.
The owner provisioned the cognee spine on db-host on 9 July 2026.
The 3D printer is an Ender-3 driven through Moonraker.
"""

WORK_DOC = """# Acme Health governance call
Sam Patel owns the scheduling workstream and must deliver the Northgate deadline.
Dr Okafor raised the Ledgerly on-premise versus cloud blocker before the refresh.
Alex committed to two actions by 16 April.
"""


async def main() -> int:
    eng = get_embedding_engine()
    assert type(eng).__name__ == "NomicOllamaEmbeddingEngine", type(eng).__name__
    assert eng.get_vector_size() == 768
    print(f"[engine] {type(eng).__name__} dims={eng.get_vector_size()} "
          f"endpoint={os.environ['EMBEDDING_ENDPOINT']}")

    t0 = time.time()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    print(f"[prune] {time.time()-t0:.0f}s")

    await cognee.add(PERSONAL_DOC, dataset_name=PERSONAL)
    await cognee.add(WORK_DOC, dataset_name=WORK)
    print("[add] 2 docs into 2 estate datasets")

    tc = time.time()
    await cognee.cognify(datasets=[PERSONAL, WORK])
    print(f"[cognify] {time.time()-tc:.0f}s")

    async def ask(dataset, q, qtype=SearchType.GRAPH_COMPLETION):
        """Return only the ANSWER payload.

        With ENABLE_BACKEND_ACCESS_CONTROL=True cognee wraps results in a
        per-dataset envelope {dataset_id, dataset_name, search_result}. Asserting
        against the stringified envelope gives false positives (the dataset name
        itself is not recalled content), so unwrap to `search_result`.
        """
        r = await cognee.search(query_text=q, query_type=qtype, datasets=[dataset], top_k=5)
        items = r if isinstance(r, list) else [r]
        out = []
        for item in items:
            if isinstance(item, dict) and "search_result" in item:
                # never let a foreign dataset's payload through, whatever cognee returns
                if item.get("dataset_name") not in (None, dataset):
                    raise AssertionError(
                        f"cognee returned dataset {item.get('dataset_name')!r} "
                        f"for a query scoped to {dataset!r}"
                    )
                out.append(str(item["search_result"]))
            else:
                out.append(str(item))
        return " ".join(out)

    failures = 0

    a = await ask(WORK, "Who owns the scheduling workstream?")
    ok = "patel" in a.lower()
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} [work graph] {a[:130]!r}")

    b = await ask(PERSONAL, "Which host runs the Postgres instance?")
    ok = "db-host" in b.lower()
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} [personal graph] {b[:130]!r}")

    # Estate isolation: the personal dataset must not know about work entities.
    c = await ask(PERSONAL, "Who owns the scheduling workstream?")
    leaked = "patel" in c.lower()
    failures += leaked
    print(f"{'OK  ' if not leaked else 'FAIL'} [estate isolation] personal cannot see "
          f"work: {c[:110]!r}")

    d = await ask(WORK, "Ledgerly blocker", SearchType.CHUNKS)
    ok = "ledgerly" in d.lower()
    failures += not ok
    print(f"{'OK  ' if ok else 'FAIL'} [chunks/pgvector] {d[:110]!r}")

    print(f"\n{'STAGE A PASS' if not failures else f'{failures} FAILURE(S)'} "
          f"(total {time.time()-t0:.0f}s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
