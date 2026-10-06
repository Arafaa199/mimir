#!/usr/bin/env python
"""LLM-judge-with-ground-truth. Scores each contender's evidence 0/1/2 vs the
confirmed ground-truth answer (pre-registered metric). Gemini flash-lite judge,
temperature 0. results_raw.json -> results_scored.json."""
import json
import re
import time
from pathlib import Path

import bakeoff_lib as L

HERE = Path(__file__).parent

JUDGE_SYS = (
    "You are a strict retrieval judge. You are given a QUERY, the correct GROUND-TRUTH "
    "answer, and EVIDENCE a retrieval system returned. Score how well the evidence "
    "contains or directly supports the correct answer:\n"
    "2 = HIT: the correct answer is clearly present or directly derivable from the evidence.\n"
    "1 = PARTIAL: some correct elements are present but incomplete, or mixed with notable wrong info.\n"
    "0 = MISS: the correct answer is not present in the evidence.\n"
    "Judge ONLY whether the ground-truth answer is supported by the evidence — ignore writing "
    'style and verbosity. Respond with STRICT JSON only: {"score": 0|1|2, "reason": "<=15 words"}.'
)


def judge(query: str, gt: str, evidence: str):
    if not evidence.strip():
        return 0, "empty evidence"
    user = (f"QUERY: {query}\n\nGROUND-TRUTH ANSWER: {gt}\n\n"
            f"EVIDENCE:\n{evidence[:6000]}\n\nScore (JSON only):")
    raw = L.gemini_chat(JUDGE_SYS, user, temperature=0.0, max_tokens=200)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0))
        sc = int(d["score"])
        return (sc if sc in (0, 1, 2) else 0), str(d.get("reason", ""))[:120]
    except Exception:  # noqa: BLE001
        return 0, f"parse-fail: {raw[:60]}"


def ev_a(it):
    return "\n---\n".join(f"[{c['source']}] {c['content'][:800]}" for c in it["A"]) or ""


def ev_bc(it):
    return "\n---\n".join(c.get("content", "")[:800] for c in it["B_chunks"] if "content" in c) or ""


def main():
    results = json.loads((HERE / "results_raw.json").read_text())
    scored = []
    for it in results:
        sa = judge(it["query"], it["ground_truth"], ev_a(it))
        sbc = judge(it["query"], it["ground_truth"], ev_bc(it))
        sbg = judge(it["query"], it["ground_truth"], it.get("B_graph", "") or "")
        scored.append({
            "id": it["id"], "class": it["class"], "query": it["query"],
            "ground_truth": it["ground_truth"],
            "score_A": sa[0], "score_B_chunks": sbc[0], "score_B_graph": sbg[0],
            "reason_A": sa[1], "reason_B_chunks": sbc[1], "reason_B_graph": sbg[1],
        })
        print(f"  {it['id']:<4} A={sa[0]} B_chunks={sbc[0]} B_graph={sbg[0]}", flush=True)
        time.sleep(1.0)
    (HERE / "results_scored.json").write_text(json.dumps(scored, indent=2))
    print(f"[judge] {len(scored)} queries scored -> results_scored.json")


if __name__ == "__main__":
    main()
