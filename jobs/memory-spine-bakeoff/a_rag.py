#!/usr/bin/env python
"""Fairness baseline: A+RAG. Feed contender A's top-5 retrieved chunks to the
SAME synthesis LLM cognee-GRAPH uses (OpenRouter gemini-2.5-flash) and ask it to
answer. Isolates the graph's contribution: cognee-GRAPH vs A+RAG holds the
synthesis LLM constant, so any remaining gap is the knowledge graph, not the LLM.
results_A.json -> results_A_rag.json."""
import json
from pathlib import Path

import bakeoff_lib as L

HERE = Path(__file__).parent
MODEL = "google/gemini-2.5-flash"
SYS = ("You answer the QUESTION using ONLY the provided CONTEXT snippets retrieved "
       "from the user's work notes. Be specific and concise. If the answer is not "
       "in the context, say what partial information is present. Do not invent facts.")


def main():
    a = json.loads((HERE / "results_A.json").read_text())
    out = []
    for it in a:
        ctx = "\n---\n".join(f"[{c['source']}] {c['text']}" for c in it["top5"])
        ans = L.openrouter_chat(
            MODEL, SYS,
            f"QUESTION: {it['query']}\n\nCONTEXT:\n{ctx[:7000]}\n\nAnswer:",
            temperature=0.0, max_tokens=400,
        )
        out.append({"id": it["id"], "class": it["class"], "query": it["query"],
                    "ground_truth": it["ground_truth"], "A_rag": ans})
        print(f"  {it['id']:<4} {len(ans)}c", flush=True)
    (HERE / "results_A_rag.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[a_rag] {len(out)} -> results_A_rag.json")


if __name__ == "__main__":
    main()
