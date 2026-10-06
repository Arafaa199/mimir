#!/usr/bin/env python
"""Does the cheaper cognify model preserve the graph win? (spec 05, budget gate)

The phase-1 bake-off measured cognee's +71% relational/temporal advantage using
OpenRouter **gemini-2.5-flash** for ECL extraction. Backfilling the real corpus on
that model costs ~$164; **gemini-2.5-flash-lite** costs ~$29 for the same 25 811
chunks. That saving is only real if the cheaper model still builds a graph as good.

So: re-run the bake-off's exact corpus (7 dense client-work docs) and its exact 16 queries
against a graph cognified by the candidate model, judge with the SAME standard, and
compare to the recorded cognee-GRAPH baseline. The judge is held constant and is a
different, stronger model than the one under test.

Judge drift is the trap here. The bake-off scored with Claude; scoring the same
cognee answers with gemini gives 0.94 instead of 1.69 overall. Comparing a
gemini-judged candidate against Claude-judged baselines is meaningless, so this
harness RE-JUDGES the bake-off's saved answers (results_A.json, results_B.json) with
the very same judge it uses on the candidate. Every number below comes from one judge.

  COGNEE_LLM_MODEL=openrouter/google/gemini-2.5-flash-lite \
    ./with_env_prod.sh ./venv/bin/python validate_model.py
  ./with_env_prod.sh ./venv/bin/python validate_model.py --reuse-candidate  # re-score only
"""
import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import cost_meter
import nomic_engine

cost_meter.install()
nomic_engine.install()

import cognee  # noqa: E402
from cognee.modules.search.types import SearchType  # noqa: E402

HERE = Path(__file__).parent
BAKEOFF = HERE.parent / "memory-spine-bakeoff"
WORK = Path.home() / "Documents" / "Work"
DATASET = os.environ.get("VALIDATE_DATASET", "model_probe")
JUDGE_MODEL = "google/gemini-2.5-flash"   # constant, stronger than the model under test
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# The judge is not deterministic even at temperature 0 (OpenRouter routes across
# providers). Re-scoring one fixed set of answers gave rel+temp 0.75/0.75/0.75/0.88
# — a spread of one flipped query (0.125). A single-shot score cannot resolve a 10%
# gate, so every item is judged JUDGE_REPEATS times and the median is taken.
JUDGE_REPEATS = int(os.environ.get("JUDGE_REPEATS", "3"))

JUDGE_SYSTEM = (
    "You grade a retrieval system's answer against ground truth. Output ONLY a "
    "single digit.\n"
    "2 = the answer contains all the ground-truth facts.\n"
    "1 = it contains some but not all, or is correct but incomplete.\n"
    "0 = it contains none of them, is wrong, or says it does not know.\n"
    "Ignore style and length. Grade only the facts."
)


def judge(query, ground_truth, answer) -> int:
    """Median of JUDGE_REPEATS independent gradings."""
    scores = sorted(_judge_once(query, ground_truth, answer) for _ in range(JUDGE_REPEATS))
    return scores[len(scores) // 2]


def _judge_once(query, ground_truth, answer) -> int:
    key = os.environ["OPENROUTER_API_KEY"]
    user = (f"QUESTION: {query}\n\nGROUND TRUTH: {ground_truth}\n\n"
            f"SYSTEM ANSWER:\n{str(answer)[:6000]}\n\nScore (0, 1, or 2):")
    body = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": 8,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        txt = json.loads(r.read())["choices"][0]["message"]["content"]
    for ch in txt:
        if ch in "012":
            return int(ch)
    return 0


def chunk_text(top5) -> str:
    return "\n".join((x.get("text") or x.get("chunk") or "") if isinstance(x, dict) else str(x)
                      for x in top5)


def unwrap(result) -> str:
    items = result if isinstance(result, list) else [result]
    out = []
    for item in items:
        if isinstance(item, dict) and "search_result" in item:
            out.append(str(item["search_result"]))
        else:
            out.append(str(item))
    return "\n".join(out)


async def candidate_answers(files, queries) -> tuple[list, float]:
    t0 = time.time()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    for rel in files:
        text = (WORK / rel).read_text(errors="ignore")
        await cognee.add(f"# SOURCE: {rel}\n\n{text}", dataset_name=DATASET)
    await cognee.cognify(datasets=[DATASET], incremental_loading=True)
    cognify_s = time.time() - t0
    print(f"[cognify] {cognify_s:.0f}s")
    cost_meter.report(docs=len(files))

    out = []
    for q in queries:
        try:
            r = await cognee.search(query_text=q["query"],
                                    query_type=SearchType.GRAPH_COMPLETION,
                                    datasets=[DATASET], top_k=5)
            out.append({"id": q["id"], "answer": unwrap(r)})
        except Exception as e:  # noqa: BLE001
            out.append({"id": q["id"], "answer": f"ERROR {type(e).__name__}: {str(e)[:100]}"})
    return out, cognify_s


async def main() -> int:
    reuse = "--reuse-candidate" in sys.argv
    model = os.environ.get("COGNEE_LLM_MODEL", "(default)")
    queries = json.loads((BAKEOFF / "queries_draft.json").read_text())
    files = [ln.strip() for ln in (BAKEOFF / "corpus_final.txt").read_text().splitlines()
             if ln.strip()]

    print(f"model under test: {model}")
    print(f"corpus: {len(files)} docs   queries: {len(queries)}   judge: {JUDGE_MODEL}\n")

    state = HERE / ".state" / "model_validation.json"
    if reuse and state.exists():
        saved = json.loads(state.read_text())
        cand = [{"id": r["id"], "answer": r["answer"]} for r in saved["rows"]]
        cognify_s = saved.get("cognify_seconds", 0.0)
        print("[reuse] scoring the saved candidate answers; no cognify")
    else:
        cand, cognify_s = await candidate_answers(files, queries)
    cand_by_id = {c["id"]: c["answer"] for c in cand}

    # Baselines: the bake-off's SAVED ANSWERS, re-judged by THIS judge.
    flash_ans = {r["id"]: r["B_graph"] for r in
                 json.loads((BAKEOFF / "results_B.json").read_text())}
    incumbent_ans = {r["id"]: chunk_text(r["top5"]) for r in
                     json.loads((BAKEOFF / "results_A.json").read_text())}

    rows = []
    for q in queries:
        qid = q["id"]
        row = {
            "id": qid, "class": q["class"],
            "candidate": judge(q["query"], q["ground_truth"], cand_by_id[qid]),
            "cognee_flash": judge(q["query"], q["ground_truth"], flash_ans[qid]),
            "incumbent": judge(q["query"], q["ground_truth"], incumbent_ans[qid]),
            "answer": cand_by_id[qid][:400],
        }
        rows.append(row)
        print(f"  {qid:<4} {q['class']:<12} candidate={row['candidate']} "
              f"cognee-flash={row['cognee_flash']} incumbent={row['incumbent']}", flush=True)

    DISC = ("relational", "temporal")

    def mean(key, classes=None):
        v = [r[key] for r in rows if classes is None or r["class"] in classes]
        return sum(v) / len(v) if v else 0.0

    print(f"\n=== all systems, ONE judge ({JUDGE_MODEL}), n={len(rows)} ===")
    print(f"{'class':<16}{'incumbent':>11}{'cognee flash':>14}{'candidate':>12}")
    for cls in ("factual", "relational", "temporal", "cross-source"):
        print(f"{cls:<16}{mean('incumbent',[cls]):>11.2f}"
              f"{mean('cognee_flash',[cls]):>14.2f}{mean('candidate',[cls]):>12.2f}")
    print(f"{'DISCRIM rel+temp':<16}{mean('incumbent',DISC):>11.2f}"
          f"{mean('cognee_flash',DISC):>14.2f}{mean('candidate',DISC):>12.2f}")
    print(f"{'ALL':<16}{mean('incumbent'):>11.2f}"
          f"{mean('cognee_flash'):>14.2f}{mean('candidate'):>12.2f}")

    inc, fl, cd = mean("incumbent", DISC), mean("cognee_flash", DISC), mean("candidate", DISC)
    print(f"\ncandidate vs incumbent on rel+temp: {(cd/inc-1)*100:+.0f}%" if inc else "")
    print(f"candidate vs cognee-flash on rel+temp: {(cd/fl-1)*100:+.0f}%" if fl else "")

    (HERE / ".state").mkdir(exist_ok=True)
    state.write_text(json.dumps(
        {"model": model, "judge": JUDGE_MODEL, "cognify_seconds": cognify_s,
         "cost": cost_meter.snapshot(),
         "rows": [{**r, "score": r["candidate"]} for r in rows]}, indent=2))

    # One flipped query out of the 8 in the rel+temp band moves the mean by 0.125,
    # and that is the judge's own measured reproducibility. The gate must not be
    # tighter than the instrument, so allow a one-flip deficit against cognee-flash.
    ONE_FLIP = 0.125
    ok = cd >= fl - ONE_FLIP and cd >= inc * 1.3
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'} — candidate must be within one "
          f"judge-flip ({ONE_FLIP}) of cognee-flash on rel+temp AND beat the "
          f"incumbent by >=30%")
    print(f"  candidate={cd:.2f}  cognee-flash={fl:.2f}  incumbent={inc:.2f}  "
          f"(judge median of {JUDGE_REPEATS})")
    print("n=16 on one corpus. Directional, not a precision instrument.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
