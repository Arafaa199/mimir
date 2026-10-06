#!/usr/bin/env python
"""Shared bake-off helpers: pg connection, nomic-768 embeddings via worker
ollama, a markdown chunker, and LLM chat (ollama + OpenRouter) for the judge.
All connection params come from the sourced bakeoff.env (BAKEOFF_* vars)."""
import json
import os
import re
import time
import urllib.error
import urllib.request

import psycopg2

OLLAMA = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def pg(db: str):
    return psycopg2.connect(
        host=os.environ["BAKEOFF_PGHOST"], port=os.environ["BAKEOFF_PGPORT"],
        user=os.environ["BAKEOFF_PGUSER"], password=os.environ["BAKEOFF_PGPASSWORD"],
        dbname=db,
    )


def embed(text: str, retries: int = 3):
    """nomic-embed-text 768-dim via ollama /api/embed. None on failure."""
    body = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    for a in range(retries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            e = d.get("embeddings") or []
            if e and len(e[0]) == 768:
                return e[0]
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            if a == retries - 1:
                print(f"[embed] failed: {ex}")
            time.sleep(1.5 * (a + 1))
    return None


def vec_literal(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (---\\n...\\n---). It is tag-dense
    and pollutes retrieval (keyword-heavy, answer-free). Applied to BOTH
    contenders for fairness."""
    m = re.match(r"^﻿?---\s*\n.*?\n---\s*\n", text, re.S)
    return text[m.end():] if m else text


def chunk_markdown(text: str, target: int = 1200, overlap: int = 150):
    """Paragraph-packed chunks (~target chars) with a small carried overlap.
    Same chunker feeds contender A; cognee chunks the same source docs itself."""
    text = strip_frontmatter(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        # a single giant paragraph -> hard-split it
        while len(p) > target * 1.6:
            head, p = p[:target], p[target - overlap:]
            chunks.append((buf + "\n\n" + head).strip() if buf else head)
            buf = ""
        if buf and len(buf) + len(p) + 2 > target:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n\n" + p) if tail else p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]


def title_of(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else fallback)[:200]


def gemini_chat(system: str, user: str, model: str = "gemini-2.5-flash-lite",
                temperature: float = 0.0, max_tokens: int = 800, retries: int = 6) -> str:
    """Gemini free-tier via raw REST (flash-lite has a healthy free quota).
    Honours 429 retryDelay. Used by the LLM judge + query helpers."""
    key = os.environ["GEMINI_API_KEY"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={key}")
    body = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }).encode()
    for a in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            return d["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < retries - 1:
                time.sleep(min(20, 5 * (a + 1))); continue
            raise
        except (KeyError, IndexError):
            return ""   # safety block / empty candidate
    raise RuntimeError("Gemini exhausted retries")


def openrouter_chat(model: str, system: str, user: str, temperature: float = 0.0,
                    max_tokens: int = 1200, retries: int = 5) -> str:
    key = os.environ["OPENROUTER_API_KEY"]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode()
    for a in range(retries):
        try:
            req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
            return out["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < retries - 1:
                wait = int(e.headers.get("Retry-After", "15") or 15)
                time.sleep(min(wait, 30)); continue
            raise
    raise RuntimeError("OpenRouter exhausted retries")
