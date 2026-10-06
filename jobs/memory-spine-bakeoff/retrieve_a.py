#!/usr/bin/env python
"""Contender A retrieval only (no LLM needed): top-5 per query from
bakeoff.hybrid_search. Dumps compact evidence for manual/self scoring."""
import json
import os
from pathlib import Path

import bakeoff_lib as L

HERE = Path(__file__).parent
K = 5


def main():
    queries = json.loads((HERE / "queries_draft.json").read_text())
    out = []
    for q in queries:
        vec = L.embed(q["query"])
        conn = L.pg(os.environ["BAKEOFF_DB_A"])
        cur = conn.cursor()
        cur.execute(
            "SELECT source_id, chunk_index, content, combined_score "
            "FROM bakeoff.hybrid_search(%s, %s, %s::vector)",
            (q["query"], K, L.vec_literal(vec) if vec else None),
        )
        rows = cur.fetchall()
        conn.close()
        out.append({
            "id": q["id"], "class": q["class"], "query": q["query"],
            "ground_truth": q["ground_truth"],
            "top5": [{"source": Path(r[0]).name, "chunk": r[1],
                      "score": round(float(r[3]), 3),
                      "text": r[2][:600]} for r in rows],
        })
    (HERE / "results_A.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    # compact console view
    for it in out:
        print(f"\n### {it['id']} [{it['class']}] {it['query']}")
        print(f"GT: {it['ground_truth']}")
        for i, c in enumerate(it["top5"], 1):
            snip = c["text"].replace("\n", " ")[:150]
            print(f"  {i}. ({c['score']}) [{c['source']}#{c['chunk']}] {snip}")
    print(f"\n[retrieve_a] {len(out)} queries -> results_A.json")


if __name__ == "__main__":
    main()
