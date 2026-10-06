#!/usr/bin/env python
"""Reproduce the cognee<->ollama-nomic embedding failure (spec 05, gotcha #3).

Exercises OllamaEmbeddingEngine.embed_text() with the input shapes cognee's
ECL pipeline actually produces for data points: empty / whitespace-only names,
huge chunk bodies, and full-size batches. Prints the true exception chain so
the fix targets the real cause instead of the symptom (EmbeddingException).
"""
import asyncio
import os
import sys
import time
import traceback

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TELEMETRY_DISABLED", "1")
os.environ.setdefault("LITELLM_LOG", "ERROR")

from cognee.infrastructure.databases.vector.embeddings.OllamaEmbeddingEngine import (  # noqa: E402
    OllamaEmbeddingEngine,
)

ENDPOINT = os.environ.get("OLLAMA_EMBED_ENDPOINT", "http://localhost:11434/api/embed")


def engine(batch_size: int = 100) -> OllamaEmbeddingEngine:
    return OllamaEmbeddingEngine(
        model="nomic-embed-text:latest",
        dimensions=768,
        max_completion_tokens=1024,
        endpoint=ENDPOINT,
        huggingface_tokenizer="nomic-ai/nomic-embed-text-v1.5",
        batch_size=batch_size,
    )


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
}


async def run(name, texts):
    eng = engine()
    t0 = time.time()
    try:
        vecs = await eng.embed_text(list(texts))
        dims = {len(v) for v in vecs}
        zeros = sum(1 for v in vecs if not any(v))
        print(f"OK   {name:<20} n={len(vecs):<4} dims={dims} zero_vecs={zeros} "
              f"{time.time()-t0:.1f}s", flush=True)
    except Exception as e:  # noqa: BLE001
        cause = e.__cause__
        print(f"FAIL {name:<20} {type(e).__name__}: {str(e)[:90]} "
              f"{time.time()-t0:.1f}s", flush=True)
        while cause is not None:
            print(f"       caused by {type(cause).__name__}: {str(cause)[:160]}", flush=True)
            cause = cause.__cause__
        if os.environ.get("REPRO_TRACE"):
            traceback.print_exc()


async def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, texts in CASES.items():
        if only and only not in name:
            continue
        await run(name, texts)


if __name__ == "__main__":
    asyncio.run(main())
