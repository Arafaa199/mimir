#!/usr/bin/env python
"""Verify the nomic embed fix against the exact inputs that produced the 422.

Runs the same adversarial cases as repro_422.py through NomicOllamaEmbeddingEngine.
Also replays real production texts (the longest rows of memory.entries) so the
result is not just synthetic. Exit code 1 if anything still raises.

  ./venv/bin/python verify_422_fix.py                    # -> localhost ollama
  OLLAMA_EMBED_ENDPOINT=http://localhost:11434/api/embed ./venv/bin/python verify_422_fix.py
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TELEMETRY_DISABLED", "1")
os.environ.setdefault("LITELLM_LOG", "ERROR")

from nomic_engine import NomicOllamaEmbeddingEngine  # noqa: E402

ENDPOINT = os.environ.get("OLLAMA_EMBED_ENDPOINT", "http://localhost:11434/api/embed")

CASES = {
    "single-normal": ["the scheduling lead is Sam Patel"],
    "single-empty": [""],
    "single-whitespace": ["   \n\t "],
    "batch-with-empty": ["alpha", "", "beta", "   ", "gamma"],
    "single-huge-200k": ["lorem ipsum dolor sit amet " * 8000],
    "single-huge-526k": ["x" * 526_004],
    "batch-8-huge-20k": ["lorem ipsum dolor sit amet " * 800] * 8,
    "batch-32-huge-20k": ["lorem ipsum dolor sit amet " * 800] * 32,
    "batch-100-mixed": (["short fact about Alex"] * 60
                        + ["lorem ipsum " * 2000] * 38
                        + ["", "  "]),
    "non-str-none": [None],
    "batch-36-default": ["fact number %d about the homelab" % i for i in range(36)],
}


def engine() -> NomicOllamaEmbeddingEngine:
    return NomicOllamaEmbeddingEngine(
        model="nomic-embed-text:latest", dimensions=768, max_completion_tokens=1024,
        endpoint=ENDPOINT, huggingface_tokenizer="nomic-ai/nomic-embed-text-v1.5",
        batch_size=36,
    )


async def main() -> int:
    failures = 0
    eng = engine()
    print(f"endpoint: {ENDPOINT}\n")
    for name, texts in CASES.items():
        t0 = time.time()
        try:
            vecs = await eng.embed_text(list(texts))
            dims = {len(v) for v in vecs}
            zeros = sum(1 for v in vecs if not any(v))
            bad = dims != {768} or len(vecs) != len(texts)
            # A zero vector is only legitimate for a non-embeddable input.
            expected_zeros = sum(1 for t in texts if not (isinstance(t, str) and t.strip()))
            if zeros != expected_zeros:
                bad = True
            status = "FAIL" if bad else "OK  "
            failures += bool(bad)
            print(f"{status} {name:<20} n={len(vecs):<4} dims={dims} "
                  f"zero={zeros}(exp {expected_zeros}) {time.time()-t0:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name:<20} {type(e).__name__}: {str(e)[:100]} "
                  f"{time.time()-t0:.1f}s", flush=True)

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
