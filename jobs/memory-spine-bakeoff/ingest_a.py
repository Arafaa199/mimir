#!/usr/bin/env python
"""Contender A ingest: freeze the snapshot -> chunk -> nomic-768 embed -> load
into bakeoff.chunks (mimir_bakeoff). Also writes the frozen corpus/ dir that
cognee ingests, so BOTH contenders see identical source docs."""
import os
import time
from pathlib import Path

import bakeoff_lib as L

WORK = Path.home() / "Documents" / "Work"
HERE = Path(__file__).parent
CORPUS = HERE / "corpus"


def main():
    files = [l.strip() for l in (HERE / "corpus_final.txt").read_text().splitlines() if l.strip()]
    conn = L.pg(os.environ["BAKEOFF_DB_A"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute((HERE / "schema_a.sql").read_text())
    print("schema applied")

    total = 0
    t0 = time.time()
    for rel in files:
        text = (WORK / rel).read_text(errors="ignore")
        dest = CORPUS / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)                      # frozen snapshot (shared input)
        title = L.title_of(text, Path(rel).stem)
        chunks = L.chunk_markdown(text)
        for i, ch in enumerate(chunks):
            emb = L.embed(ch)
            cur.execute(
                "INSERT INTO bakeoff.chunks (source_id, chunk_index, title, content, embedding, metadata) "
                "VALUES (%s,%s,%s,%s,%s::vector,%s::jsonb)",
                (rel, i, title, ch, L.vec_literal(emb) if emb else None, "{}"),
            )
        total += len(chunks)
        print(f"  {len(chunks):3d} chunks  {rel}")
    cur.execute("SELECT count(*), count(embedding) FROM bakeoff.chunks")
    rows, embs = cur.fetchone()
    print(f"\nTOTAL {total} chunks from {len(files)} docs in {time.time()-t0:.0f}s "
          f"| rows={rows} embedded={embs}")
    conn.close()


if __name__ == "__main__":
    main()
