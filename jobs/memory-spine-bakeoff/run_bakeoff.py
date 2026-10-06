#!/usr/bin/env python
"""Retrieval pass: run every query through both contenders, dump raw results.
  A          — bakeoff.hybrid_search top-5 (the pgvector-hybrid incumbent)
  B_chunks   — cognee SearchType.CHUNKS top-5 (retrieval parity with A)
  B_graph    — cognee SearchType.GRAPH_COMPLETION (its graph-augmented answer)
Run via with_env.sh (LLM_BACKEND=gemini). Output -> results_raw.json."""
import asyncio
import json
import os
import time
from pathlib import Path

import cognee
from cognee.modules.search.types import SearchType

import bakeoff_lib as L

HERE = Path(__file__).parent
DATASET = "work"
K = 5


def _text(item) -> str:
    """Best-effort text out of a cognee result item (dict / pydantic / str)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("text", "content", "chunk", "document"):
            if item.get(key):
                return str(item[key])
        return json.dumps(item, default=str)
    for attr in ("text", "content", "payload"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    return str(item)


def contender_a(query: str):
    vec = L.embed(query)
    conn = L.pg(os.environ["BAKEOFF_DB_A"])
    cur = conn.cursor()
    cur.execute(
        "SELECT source_id, chunk_index, title, content, text_score, vector_score, combined_score "
        "FROM bakeoff.hybrid_search(%s, %s, %s::vector)",
        (query, K, L.vec_literal(vec) if vec else None),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"source": r[0], "chunk": r[1], "title": r[2], "content": r[3],
             "text_score": float(r[4]), "vector_score": float(r[5]), "combined": float(r[6])}
            for r in rows]


async def contender_b_chunks(query: str):
    try:
        r = await cognee.search(query_text=query, query_type=SearchType.CHUNKS,
                                datasets=[DATASET], top_k=K)
    except Exception as e:  # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {str(e)[:150]}"}]
    return [{"content": _text(it)[:1500]} for it in (r or [])[:K]]


async def contender_b_graph(query: str):
    try:
        r = await cognee.search(query_text=query, query_type=SearchType.GRAPH_COMPLETION,
                                datasets=[DATASET], top_k=K)
    except Exception as e:  # noqa: BLE001
        return f"ERROR {type(e).__name__}: {str(e)[:150]}"
    if isinstance(r, list):
        return "\n".join(_text(x) for x in r)
    return _text(r)


async def main():
    queries = json.loads((HERE / "queries.json").read_text())
    out = []
    t0 = time.time()
    for q in queries:
        a = contender_a(q["query"])
        bc = await contender_b_chunks(q["query"])
        bg = await contender_b_graph(q["query"])
        out.append({**q, "A": a, "B_chunks": bc, "B_graph": bg})
        print(f"  {q['id']:<4} {q['class']:<11} done", flush=True)
    (HERE / "results_raw.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"[run] {len(out)} queries in {time.time()-t0:.0f}s -> results_raw.json")


if __name__ == "__main__":
    asyncio.run(main())
