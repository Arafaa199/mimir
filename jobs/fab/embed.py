#!/usr/bin/env python3
"""fab.embed — embed a prompt for the model cache (matches the memory spine).

Uses the same Ollama model the memory spine uses (nomic-embed-text, 768-dim),
so cache vectors live in the same space as memory.entries. Best-effort: returns
None when the embedding service is unreachable, and the cache falls back to a
trigram match on the prompt text (see fab.db.cache_search).
"""

import json
import urllib.error
import urllib.request


def embed(text: str, cfg: dict) -> list[float] | None:
    ec = cfg.get("embedding", {})
    url = ec.get("url", "http://localhost:11434").rstrip("/") + "/api/embeddings"
    body = json.dumps({"model": ec.get("model", "nomic-embed-text"),
                       "prompt": text}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=ec.get("timeout_s", 20)) as resp:
            out = json.loads(resp.read().decode())
        vec = out.get("embedding")
        if isinstance(vec, list) and len(vec) == ec.get("dim", 768):
            return vec
        print(f"[fab.embed] unexpected embedding dim "
              f"{len(vec) if isinstance(vec, list) else 'n/a'} — skipping", flush=True)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError) as e:
        print(f"[fab.embed] embedding unavailable ({e}) — trigram fallback", flush=True)
    return None
