#!/usr/bin/env python
"""Smoke test: prove cognee add->cognify->search works end-to-end on the
bake-off stack (db-host pgvector + embedded kuzu + worker ollama qwen), with NO
paid API. Times cognify so we can size the real corpus. Run via with_env.sh."""
import asyncio
import time

import cognee
from cognee.modules.search.types import SearchType

DOC = (
    "Acme Health Governance — 2026-04-09.\n"
    "Dr Okafor is the client governance lead for the Acme Health project. "
    "On 2026-04-09 the Acme Health governance call approved the Platform Modernisation Proposal. "
    "Alex committed to send Dr Okafor the revised staffing cost model by 2026-04-15. "
    "The Contractor Hours reconciliation is blocked by missing Ledgerly exports from the client finance team."
)


async def main():
    t0 = time.time()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    print(f"[prune] {time.time()-t0:.1f}s")

    t1 = time.time()
    await cognee.add(DOC, dataset_name="smoke")
    print(f"[add] {time.time()-t1:.1f}s")

    t2 = time.time()
    await cognee.cognify(datasets=["smoke"])
    print(f"[cognify] {time.time()-t2:.1f}s  (per-doc cost signal)")

    for st in (SearchType.CHUNKS, SearchType.GRAPH_COMPLETION, SearchType.INSIGHTS
               if hasattr(SearchType, "INSIGHTS") else SearchType.TRIPLET_COMPLETION):
        t3 = time.time()
        try:
            r = await cognee.search(
                query_text="What did Alex commit to and by when?",
                query_type=st, datasets=["smoke"], top_k=5,
            )
            print(f"\n=== {st.value} ({time.time()-t3:.1f}s) ===")
            print(str(r)[:600])
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {st.value} FAILED: {type(e).__name__}: {str(e)[:200]} ===")
    print(f"\n[total] {time.time()-t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
