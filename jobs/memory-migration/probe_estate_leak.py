#!/usr/bin/env python
"""Does cognee's `datasets=[...]` actually scope retrieval? (spec 05 §4)

Estate integrity is load-bearing: work must never enter personal recall. This
probe is the evidence for that property, run as a matched pair.

  ENABLE_BACKEND_ACCESS_CONTROL=False  -> the dataset argument is IGNORED.
      cognee's search.py takes the `else` branch and "runs search without setting
      database context", against one global vector+graph store.
  ENABLE_BACKEND_ACCESS_CONTROL=True   -> each dataset gets its own vector+graph
      database and the scope is physical.

Markers are entities that appear ONLY in the corpus and never in the question, so
an echoed question cannot be mistaken for a leak.

Assumes smoke_prod.py has populated smoke_personal / smoke_work.

  ENABLE_BACKEND_ACCESS_CONTROL=False ./with_env_prod.sh ./venv/bin/python probe_estate_leak.py
  ENABLE_BACKEND_ACCESS_CONTROL=True  ./with_env_prod.sh ./venv/bin/python probe_estate_leak.py
"""
import asyncio
import os
import sys

import nomic_engine

nomic_engine.install()

import cognee  # noqa: E402
from cognee.modules.search.types import SearchType  # noqa: E402

PERSONAL = "smoke_personal"
WORK = "smoke_work"

# entities exclusive to each corpus, absent from every question asked below.
# Synthetic: these must match the fixture documents in smoke_prod.py.
WORK_ONLY = ("patel", "okafor", "northgate", "16 april", "ledgerly")
PERSONAL_ONLY = ("ender", "moonraker", "mimir-fab")

WORK_Q = "Who owns the scheduling workstream?"
PERSONAL_Q = "Which printer is driven through the local host?"


def unwrap(result) -> str:
    """Strip cognee's per-dataset envelope; keep only recalled content."""
    items = result if isinstance(result, list) else [result]
    parts = []
    for item in items:
        if isinstance(item, dict) and "search_result" in item:
            parts.append(str(item["search_result"]))
        else:
            parts.append(str(item))
    return " ".join(parts)


def found(text, markers):
    low = text.lower()
    return sorted({m for m in markers if m in low})


async def probe(label, dataset, query, qtype, foreign_markers):
    try:
        r = await cognee.search(query_text=query, query_type=qtype,
                                datasets=[dataset] if dataset else None, top_k=5)
        blob = unwrap(r)
    except Exception as e:  # noqa: BLE001
        print(f"  {label:<30} ERROR {type(e).__name__}: {str(e)[:60]}")
        return
    leaked = found(blob, foreign_markers)
    verdict = f"LEAK {leaked}" if leaked else "clean"
    print(f"  {label:<30} {verdict}")


async def main():
    acl = os.environ.get("ENABLE_BACKEND_ACCESS_CONTROL", "?")
    print(f"ENABLE_BACKEND_ACCESS_CONTROL={acl}\n")

    for qt in (SearchType.GRAPH_COMPLETION, SearchType.CHUNKS, SearchType.SUMMARIES):
        print(f"[{qt.name}]  work question -> personal scope (must be clean)")
        await probe("datasets=[personal]", PERSONAL, WORK_Q, qt, WORK_ONLY)
        print(f"[{qt.name}]  personal question -> work scope (must be clean)")
        await probe("datasets=[work]", WORK, PERSONAL_Q, qt, PERSONAL_ONLY)
        print()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
