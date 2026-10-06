#!/usr/bin/env python
"""Stage B step 1 — build the backfill corpus. READ-ONLY on every production store.

Emits `.state/corpus.jsonl` (one document per line) plus a distribution report.
Nothing is written to memory.entries, brain.db, search.embeddings or cognee here.

Three sources, with explicit precedence so the same text is never cognified twice:

  1. vault   raw.notes_index (live notes)        — whole documents, the human brain
  2. memory  memory.entries (distinct content)   — agent memory, nomic-768
  3. brain   brain.db memories (distinct)        — ZeroClaw memory

`brain.db` category='work' is EXCLUDED: those 6 513 rows are heading-chunks of
the very Work vault notes source 1 already carries (keys look like
`Work/Docs/....md :: Executive Summary`). Cognifying both would duplicate ~10 MB
of text and split the same entities across two provenance trails. cognee chunks
documents itself, so the whole note is the better unit.

Cross-source exact duplicates are collapsed by content hash, vault > memory > brain.

ESTATES (approved 2026-07-09). A work->personal mislabel is the leak spec 05 §4
forbids; a personal->work mislabel is merely misfiled. So the fallback is `work`.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from estates import estate_for_brain, estate_for_memory, estate_for_vault, label_sections

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
OUT = STATE / "corpus.jsonl"

BRAIN_DB = "$HOME/.zeroclaw/workspace/memory/brain.db"
BRAIN_EXCLUDED_CATEGORY = "work"  # superseded by the vault notes (see docstring)

# Measured (not the bake-off's $/MB, which was ~12x optimistic): cognee's ECL makes
# ~1.3 LLM calls per ~1.2 KB chunk. See cost_meter.py and the README.
USD_PER_CHUNK = 0.4562 / 72
SEC_PER_CHUNK = (5.8 * 60) / 72
CHUNK_CHARS = 1200

def sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


def db_host_jsonl(inner_sql: str):
    """Stream rows from the prod db-host DB as JSON, read-only, over ssh.

    Keeps db-host superuser credentials out of this process entirely: psql runs
    inside the container and reads $POSTGRES_PASSWORD from its own environment.

    Each row is base64-encoded server-side. `row_to_json` alone is not safe here:
    note bodies contain carriage returns and other control characters that survive
    into psql's unaligned output and split a logical row across several lines.
    """
    sql = (f"SELECT translate(encode(convert_to(row_to_json(t)::text,'UTF8'),'base64'), E'\\n','')"
           f" FROM ({inner_sql}) t;")
    cmd = ("docker exec -i db-host-db bash -c "
           "'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U db-host -d db-host -qtAX -v ON_ERROR_STOP=1 -f -'")
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "db-host", cmd],
        input=sql, capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            yield json.loads(base64.b64decode(line).decode("utf-8", "replace"))


VAULT_SQL = """
  SELECT relative_path, title, content FROM raw.notes_index
   WHERE removed_at IS NULL AND content IS NOT NULL AND length(trim(content)) > 0
"""

MEMORY_SQL = """
  SELECT DISTINCT ON (md5(content)) content, namespace, source, category
    FROM memory.entries WHERE length(trim(content)) > 0
   ORDER BY md5(content), created_at ASC
"""


def fetch_vault():
    for r in db_host_jsonl(VAULT_SQL):
        estate, ambiguous = estate_for_vault(r["relative_path"], r["content"])
        yield {
            "source": "vault",
            "provenance": f"vault:{r['relative_path']}",
            "title": r["title"] or r["relative_path"],
            "text": r["content"],
            "estate": estate,
            "ambiguous": ambiguous,
        }


def fetch_memory():
    for r in db_host_jsonl(MEMORY_SQL):
        estate, ambiguous = estate_for_memory(r["namespace"], r["content"])
        yield {
            "source": "memory",
            "provenance": f"memory:{r['namespace']}/{r['source']}",
            "title": (r["content"].strip().splitlines() or [""])[0][:120],
            "text": r["content"],
            "estate": estate,
            "ambiguous": ambiguous,
        }


BRAIN_SCRIPT = f"""
import sqlite3, json, sys, hashlib
# read-only URI: brain.db is live (ZeroClaw writes to it); never open it rw.
c = sqlite3.connect("file:{BRAIN_DB}?mode=ro", uri=True)
seen = set()
q = "SELECT key, category, content FROM memories WHERE category != '{BRAIN_EXCLUDED_CATEGORY}'"
for key, cat, content in c.execute(q):
    if not content or not content.strip():
        continue
    # sha256, not builtin hash(): PYTHONHASHSEED randomises str hashing per process,
    # so a dedup keyed on it is neither reproducible nor collision-safe. Silently
    # dropping a memory here would be unrecoverable.
    h = hashlib.sha256(content.strip().encode("utf-8", "replace")).hexdigest()
    if h in seen:
        continue
    seen.add(h)
    sys.stdout.write(json.dumps({{"key": key, "category": cat, "content": content}}) + "\\n")
"""


def fetch_brain():
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "worker", "python3 -"],
        input=BRAIN_SCRIPT, capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        estate, ambiguous = estate_for_brain(r["key"], r["category"], r["content"])
        yield {
            "source": "brain",
            "provenance": f"brain:{r['key'][:160]}",
            "title": r["key"][:120],
            "text": r["content"],
            "estate": estate,
            "ambiguous": ambiguous,
        }


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)

    seen: dict[str, str] = {}
    docs = []
    collisions = defaultdict(int)

    split_docs = 0

    # precedence: vault > memory > brain
    for producer, label in ((fetch_vault, "vault"),
                            (fetch_memory, "memory"),
                            (fetch_brain, "brain")):
        for doc in producer():
            # Mixed documents (personal path, work content) are cut into sections and
            # each section labelled on its own; everything else stays whole.
            sections = label_sections(doc["text"], doc["estate"], doc["ambiguous"])
            if len(sections) > 1:
                split_docs += 1
            for i, sec in enumerate(sections):
                h = sha(sec.text)
                if h in seen:
                    collisions[f"{label}<-{seen[h]}"] += 1
                    continue
                seen[h] = label
                prov = doc["provenance"]
                if len(sections) > 1:
                    prov = f"{prov}#{i}:{sec.heading[:60]}" if sec.heading else f"{prov}#{i}"
                docs.append({
                    "source": doc["source"],
                    "provenance": prov,
                    "title": sec.heading or doc["title"],
                    "text": sec.text,
                    "estate": sec.estate,
                    "ambiguous": sec.ambiguous,
                    "sha256": h,
                    "chars": len(sec.text),
                })
        print(f"[{label}] cumulative unique units: {len(docs)}", flush=True)

    print(f"[split] {split_docs} mixed documents were cut into estate-labelled sections")

    with OUT.open("w") as fh:
        for d in docs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    # ---- report ----------------------------------------------------------
    by_estate = defaultdict(lambda: {"docs": 0, "chars": 0})
    by_source = defaultdict(lambda: {"docs": 0, "chars": 0})
    ambiguous = []
    for d in docs:
        by_estate[d["estate"]]["docs"] += 1
        by_estate[d["estate"]]["chars"] += d["chars"]
        by_source[d["source"]]["docs"] += 1
        by_source[d["source"]]["chars"] += d["chars"]
        if d["ambiguous"]:
            ambiguous.append(d)

    MB = 1024 * 1024
    total_chars = sum(d["chars"] for d in docs)

    def chunks_of(units):
        return sum(max(1, -(-d["chars"] // CHUNK_CHARS)) for d in units)

    print(f"\n=== corpus -> {OUT} ===")
    print(f"{'estate':<12}{'units':>8}{'MB':>9}{'chunks':>9}{'~$':>9}{'~hours':>9}")
    for est, v in sorted(by_estate.items()):
        units = [d for d in docs if d["estate"] == est]
        ch = chunks_of(units)
        print(f"{est:<12}{v['docs']:>8}{v['chars']/MB:>9.2f}{ch:>9}"
              f"{ch*USD_PER_CHUNK:>9.2f}{ch*SEC_PER_CHUNK/3600:>9.1f}")
    ch = chunks_of(docs)
    print(f"{'TOTAL':<12}{len(docs):>8}{total_chars/MB:>9.2f}{ch:>9}"
          f"{ch*USD_PER_CHUNK:>9.2f}{ch*SEC_PER_CHUNK/3600:>9.1f}")
    print(f"(gemini-2.5-flash. flash-lite is ~5.7x cheaper on this token mix: "
          f"~${ch*USD_PER_CHUNK/5.7:.2f})")

    print(f"\n{'source':<12}{'docs':>8}{'MB':>9}")
    for src, v in sorted(by_source.items()):
        print(f"{src:<12}{v['docs']:>8}{v['chars']/MB:>9.2f}")

    print("\ncross-source exact duplicates collapsed:")
    for k, n in sorted(collisions.items(), key=lambda x: -x[1]):
        print(f"  {k:<20} {n}")
    if not collisions:
        print("  (none)")

    print(f"\nAMBIGUOUS -> defaulted to work: {len(ambiguous)} docs "
          f"({sum(d['chars'] for d in ambiguous)/MB:.2f} MB)")
    groups = defaultdict(list)
    for d in ambiguous:
        key = re.sub(r"[^a-zA-Z]+.*$", "", d["provenance"].split(":", 1)[1][:40]) or d["source"]
        groups[f"{d['source']}:{key}"].append(d)
    for key, items in sorted(groups.items(), key=lambda x: -len(x[1]))[:12]:
        print(f"\n  [{key}] {len(items)} docs")
        for d in items[:3]:
            print(f"    - {d['provenance'][:100]}")
            print(f"      {d['text'].strip()[:110]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
