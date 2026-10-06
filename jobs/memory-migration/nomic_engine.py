#!/usr/bin/env python
"""cognee <-> ollama/nomic-768 embedding path — the fix for the "422" (spec 05 §3).

ROOT CAUSE (measured, not inferred — see repro_422.py):
  The 422 is NOT an HTTP status from ollama. It is the default `status_code` on
  cognee's own `EmbeddingException` (CogneeConfigurationError). Every observed
  failure is a masked `asyncio.TimeoutError`, produced by three compounding bugs
  in `OllamaEmbeddingEngine`:

  1. UNBOUNDED FAN-OUT. `embed_text()` does `asyncio.gather(*[_get_embedding(p)
     for p in texts])` over the whole batch. cognee's default batch size is 36
     (embeddings/config.py:107), so 36 requests hit ollama at once. Ollama
     serialises per model, so request #36 waits for #1..#35 to finish.
  2. HARDCODED 60 s TIMEOUT. `_get_embedding()` passes `timeout=60.0` to aiohttp
     with no way to override. Under (1) the tail of the batch always blows it.
  3. THE RESCUE PATH CAN'T SEE IT. `embed_text()` only reacts to substrings like
     "context length"/"input length" in `str(error)`. `str(TimeoutError())` is
     the EMPTY STRING, so the reactive split never fires and the timeout is
     rethrown as `EmbeddingException(..., status_code=422)`.

  Amplifier: no client-side truncation. Ollama truncates to the model's context
  window anyway (nomic n_ctx=2048), but only AFTER tokenising the full payload —
  a 200 KB data point costs ~23 s of pure tokenisation on a CPU-only host.

FIX: bound concurrency, truncate before sending, raise+configure the timeout, and
degrade a hard-failing single text to a zero vector instead of killing the batch.

Applied as a SUBCLASS + factory patch; the vendored cognee clone is never edited.
"""
import asyncio
import os
from functools import lru_cache
from importlib import import_module
from typing import List, Optional

import aiohttp

from cognee.infrastructure.databases.exceptions import EmbeddingException
from cognee.infrastructure.databases.vector.embeddings.OllamaEmbeddingEngine import (
    OllamaEmbeddingEngine,
)
from cognee.infrastructure.databases.vector.embeddings.utils import (
    handle_embedding_response,
    sanitize_embedding_text_inputs,
)
from cognee.shared.logging_utils import get_logger

logger = get_logger("NomicOllamaEmbeddingEngine")

# nomic-embed-text has n_ctx=2048. Ollama truncates server-side, so cutting the
# payload client-side is semantically free — it only removes tokens ollama would
# have dropped. 12000 chars >= 2048 tokens for any text down to 5.8 chars/token,
# which covers prose, markdown and code. It exists purely to bound tokenise cost.
MAX_INPUT_CHARS = int(os.environ.get("NOMIC_MAX_INPUT_CHARS", "12000"))
# Ollama serialises per loaded model unless OLLAMA_NUM_PARALLEL > 1. Keep the
# queue short so no request can age past the timeout waiting behind its siblings.
CONCURRENCY = int(os.environ.get("NOMIC_EMBED_CONCURRENCY", "4"))
REQUEST_TIMEOUT = float(os.environ.get("NOMIC_EMBED_TIMEOUT", "180"))
EMBED_RETRIES = int(os.environ.get("NOMIC_EMBED_RETRIES", "3"))


class NomicOllamaEmbeddingEngine(OllamaEmbeddingEngine):
    """OllamaEmbeddingEngine with bounded concurrency, truncation and a real timeout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._semaphore = asyncio.Semaphore(CONCURRENCY)

    async def embed_text(self, text: List[str]) -> List[List[float]]:
        original_texts = text if isinstance(text, list) else [text]
        sanitized = sanitize_embedding_text_inputs(original_texts)
        truncated = [t[:MAX_INPUT_CHARS] for t in sanitized]

        if self.mock:
            zeros = [[0.0] * self.dimensions for _ in truncated]
            return handle_embedding_response(original_texts, zeros, self.dimensions)

        embeddings = await asyncio.gather(
            *[self._guarded_embedding(p) for p in truncated]
        )
        return handle_embedding_response(original_texts, embeddings, self.dimensions)

    async def _guarded_embedding(self, prompt: str) -> List[float]:
        """One embedding, serialised behind the semaphore, retried with backoff.

        Fails LOUD. A zero vector is a silent, permanent hole in recall — a row
        that can never be retrieved again and looks identical to a successful
        write. Only genuinely non-embeddable inputs get zeroed, and cognee's
        `handle_embedding_response` already does that upstream of us. An infra
        failure must abort the batch so it can be retried, not be papered over.
        """
        last: Optional[BaseException] = None
        for attempt in range(EMBED_RETRIES):
            async with self._semaphore:
                try:
                    return await self._get_embedding(prompt)
                except Exception as error:  # noqa: BLE001
                    last = error
                    logger.warning(
                        "nomic embed attempt %d/%d failed (%s: %s) for %d-char input",
                        attempt + 1, EMBED_RETRIES, type(error).__name__,
                        str(error)[:120] or "<empty>", len(prompt),
                    )
            if attempt < EMBED_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)

        raise EmbeddingException(
            f"nomic embed failed after {EMBED_RETRIES} attempts for a "
            f"{len(prompt)}-char input via {self.endpoint} "
            f"(cause: {type(last).__name__}: {str(last)[:120] or '<empty>'})"
        ) from last

    async def _get_embedding(self, prompt: str) -> List[float]:
        payload = {"model": self.model, "input": prompt}
        headers = {}
        api_key = os.getenv("LLM_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.endpoint, json=payload, headers=headers) as response:
                data = await response.json()

        if "error" in data:
            raise RuntimeError(f"Ollama embedding API error: {data['error']}")

        vectors = data.get("embeddings") or ([data["embedding"]] if "embedding" in data else [])
        if not vectors:
            # ollama answers an empty/unembeddable input with `{"embeddings": []}`;
            # cognee's original code indexes [0] straight into an IndexError.
            raise ValueError(f"Ollama returned no embedding for a {len(prompt)}-char input")
        vector = vectors[0]
        if len(vector) != self.dimensions:
            raise ValueError(f"Expected {self.dimensions} dims, got {len(vector)}")
        return vector


@lru_cache
def _create_embedding_engine(provider, model, dimensions, max_completion_tokens, endpoint,
                             api_key, api_version, batch_size, huggingface_tokenizer,
                             llm_api_key, llm_provider):
    if provider == "ollama":
        return NomicOllamaEmbeddingEngine(
            model=model,
            dimensions=dimensions,
            max_completion_tokens=max_completion_tokens,
            endpoint=endpoint,
            huggingface_tokenizer=huggingface_tokenizer,
            batch_size=batch_size,
        )
    return _ORIGINAL_FACTORY(provider, model, dimensions, max_completion_tokens, endpoint,
                             api_key, api_version, batch_size, huggingface_tokenizer,
                             llm_api_key, llm_provider)


# `embeddings/__init__.py` rebinds the `get_embedding_engine` attribute to the
# FUNCTION, shadowing the submodule — so `import ...get_embedding_engine as m`
# hands back the function. Go through importlib to get the real module object.
_gee_mod = import_module(
    "cognee.infrastructure.databases.vector.embeddings.get_embedding_engine"
)
_ORIGINAL_FACTORY = _gee_mod.create_embedding_engine
_installed = False


def install() -> None:
    """Route cognee's ollama embedding provider through the fixed engine.

    Patches the factory, not the class: the local cognee checkout (pinned to
    upstream) is left byte-identical.
    """
    global _installed
    if _installed:
        return
    _gee_mod.create_embedding_engine = _create_embedding_engine
    _installed = True
    logger.info(
        "nomic embed fix installed (concurrency=%d timeout=%.0fs max_chars=%d)",
        CONCURRENCY, REQUEST_TIMEOUT, MAX_INPUT_CHARS,
    )


def engine(endpoint: Optional[str] = None) -> NomicOllamaEmbeddingEngine:
    """Standalone engine, for backfill/parity scripts that embed outside cognee."""
    return NomicOllamaEmbeddingEngine(
        model=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text:latest"),
        dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "768")),
        max_completion_tokens=1024,
        endpoint=endpoint or os.environ["EMBEDDING_ENDPOINT"],
        huggingface_tokenizer="nomic-ai/nomic-embed-text-v1.5",
        batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "16")),
    )
