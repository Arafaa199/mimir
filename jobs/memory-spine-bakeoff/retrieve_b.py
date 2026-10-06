#!/usr/bin/env python
"""Contender B (cognee) retrieval on the 16 queries: CHUNKS top-5 (retrieval
parity with A) + GRAPH_COMPLETION (graph-augmented answer). No worker needed
(OpenRouter LLM + fastembed + db-host-tailnet). Run via with_env.sh
(LLM_BACKEND=openrouter EMBED_BACKEND=fastembed). -> results_B.json."""
import asyncio
import json
from pathlib import Path

import cognee
from cognee.modules.search.types import SearchType

HERE = Path(__file__).parent
DATASET = "work"
K = 5


def _text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("text", "content", "chunk", "document"):
            if item.get(k):
                return str(item[k])
        return json.dumps(item, default=str)
    for a in ("text", "content", "payload"):
        v = getattr(item, a, None)
        if v:
            return str(v)
    return str(item)


async def chunks(query):
    try:
        r = await cognee.search(query_text=query, query_type=SearchType.CHUNKS,
                                datasets=[DATASET], top_k=K)
        return [_text(x)[:900] for x in (r or [])[:K]]
    except Exception as e:  # noqa: BLE001
        return [f"ERROR {type(e).__name__}: {str(e)[:150]}"]


async def graph(query):
    try:
        r = await cognee.search(query_text=query, query_type=SearchType.GRAPH_COMPLETION,
                                datasets=[DATASET], top_k=K)
        return "\n".join(_text(x) for x in r) if isinstance(r, list) else _text(r)
    except Exception as e:  # noqa: BLE001
        return f"ERROR {type(e).__name__}: {str(e)[:150]}"


async def main():
    queries = json.loads((HERE / "queries_draft.json").read_text())
    out = []
    for q in queries:
        bc = await chunks(q["query"])
        bg = await graph(q["query"])
        out.append({"id": q["id"], "class": q["class"], "query": q["query"],
                    "ground_truth": q["ground_truth"], "B_chunks": bc, "B_graph": bg})
        print(f"  {q['id']:<4} chunks={len(bc)} graph={len(bg)}c", flush=True)
    (HERE / "results_B.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[retrieve_b] {len(out)} queries -> results_B.json")


if __name__ == "__main__":
    asyncio.run(main())
