#!/usr/bin/env python
"""Refuse to embed into the spine from the wrong embedding space. Fail closed.

Same model name. Same digest (`0a109f422b47`). Different vectors:

    laptop (ollama 0.31.1)   vs  worker (ollama 0.16.1)   cos = 0.826
    worker                  vs  db-host                    cos = 1.000
    a STORED memory.entries vector, re-embedded on worker cos = 1.000000
    ...the same row,        re-embedded on laptop          cos = 0.869

A newer ollama changed how nomic-embed-text is pooled/normalised. Production
(`intake/handlers/memory.py`, OLLAMA_URL=localhost) embeds on **worker**, so
worker's space IS the production space. Backfilling the spine from laptop would build
a store whose vectors are 0.87-similar to every query the production API later
issues — a silent, corpus-wide retrieval regression that no test would catch,
because within a single run everything looks self-consistent.

This check pins the endpoint to production's space by re-embedding a real, stored
`memory.entries` row and comparing against its stored vector. It reads one row and
writes nothing.

  ./with_env_prod.sh ./venv/bin/python check_embed_space.py
"""
import base64
import json
import math
import os
import socket
import subprocess
import sys
import urllib.request

MIN_COS = float(os.environ.get("EMBED_SPACE_MIN_COS", "0.999"))
SAMPLE_ROWS = 3


def prod_rows(n: int):
    sql = (
        "SELECT translate(encode(convert_to(row_to_json(t)::text,'UTF8'),'base64'),E'\\n','')"
        " FROM (SELECT content, embedding::text AS emb FROM memory.entries"
        f" WHERE embedding IS NOT NULL AND length(content) BETWEEN 200 AND 3000"
        f" ORDER BY created_at DESC LIMIT {n}) t;"
    )
    inner = ("docker exec -i db-host-db bash -c "
             "'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U db-host -d db-host -qtAX -v ON_ERROR_STOP=1 -f -'")
    # This also runs ON db-host (the backfill host), where `ssh db-host` would be a loop.
    argv = (["bash", "-c", inner] if socket.gethostname() == "db-host"
            else ["ssh", "-o", "ConnectTimeout=10", "db-host", inner])
    proc = subprocess.run(argv, input=sql, capture_output=True, text=True, check=True)
    for line in proc.stdout.splitlines():
        if line.strip():
            yield json.loads(base64.b64decode(line).decode("utf-8", "replace"))


def embed(endpoint: str, text: str):
    body = json.dumps({"model": "nomic-embed-text:latest", "input": text}).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"][0]


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    endpoint = os.environ["EMBEDDING_ENDPOINT"]
    print(f"embedding endpoint: {endpoint}")
    print(f"required cosine vs production's stored vectors: >= {MIN_COS}\n")

    worst = 1.0
    for i, row in enumerate(prod_rows(SAMPLE_ROWS), 1):
        stored = [float(x) for x in row["emb"].strip("[]").split(",")]
        fresh = embed(endpoint, row["content"])
        if len(fresh) != len(stored):
            print(f"  row {i}: FAIL dimension {len(fresh)} != {len(stored)}")
            return 2
        c = cosine(stored, fresh)
        worst = min(worst, c)
        print(f"  row {i}: cos={c:.6f}  ({'ok' if c >= MIN_COS else 'MISMATCH'})")

    print()
    if worst < MIN_COS:
        print(f"REFUSING: worst cosine {worst:.6f} < {MIN_COS}.\n"
              f"This endpoint does NOT reproduce production's embedding space.\n"
              f"Production embeds on worker (ollama 0.16.1). laptop runs ollama 0.31.1,\n"
              f"which pools nomic-embed-text differently. Point EMBEDDING_ENDPOINT at\n"
              f"worker (http://localhost:11434/api/embed, or the tailnet IP off-LAN).",
              file=sys.stderr)
        return 1
    print(f"OK — endpoint reproduces production's embedding space (worst cos {worst:.6f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
