#!/usr/bin/env python
"""Contender B ingest: add the 20-doc Work snapshot to cognee + cognify
(ECL: extract entities/relations -> kuzu graph + pgvector vectors). Long-running
on free Gemini flash-lite; run backgrounded. Prints per-doc + timing for the
cost metric. Run via with_env.sh (LLM_BACKEND=gemini)."""
import asyncio
import time
from pathlib import Path

import cognee

HERE = Path(__file__).parent
WORK = Path.home() / "Documents" / "Work"
DATASET = "work"


async def main():
    files = [l.strip() for l in (HERE / "corpus_final.txt").read_text().splitlines() if l.strip()]
    t0 = time.time()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    print(f"[prune] {time.time()-t0:.0f}s  ({len(files)} docs to add)", flush=True)

    for i, rel in enumerate(files, 1):
        text = (WORK / rel).read_text(errors="ignore")
        # keep provenance in the text so it can enter the graph
        await cognee.add(f"# SOURCE: {rel}\n\n{text}", dataset_name=DATASET)
        print(f"[add {i}/{len(files)}] {rel}", flush=True)
    print(f"[add-done] {time.time()-t0:.0f}s", flush=True)

    tc = time.time()
    await cognee.cognify(datasets=[DATASET])
    print(f"[cognify-done] cognify={time.time()-tc:.0f}s total={time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
