#!/usr/bin/env bash
# Sources the ephemeral bake-off creds + exports cognee config, then execs argv.
# Keeps the DB password out of argv/stdout (only ever in-process env).
# Usage: ./with_env.sh ./venv/bin/python cognee_driver.py ...
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
# shellcheck disable=SC1091
source "$HERE/bakeoff.env"

# --- cognee: LLM backend switch ---------------------------------------------
# Empirical (2026-07-08): worker qwen2.5:7b = CPU+swap, >2min/call (no GPU) =
# untenable; OpenRouter free pool = 429 upstream; gemini-2.0-flash daily cap
# exhausted. gemini-2.5-flash-lite free tier is HEALTHY + fast => default.
LLM_BACKEND="${LLM_BACKEND:-gemini}"
LLM_RATE_LIMIT_ENABLED=true
LLM_RATE_LIMIT_INTERVAL=60
COGNEE_SKIP_CONNECTION_TEST=true          # 30s cold-probe is flaky on free tiers
if [ "$LLM_BACKEND" = "gemini" ]; then
  LLM_PROVIDER=gemini
  # flash-lite + 2.0-flash-lite hit a 20/day free cap; gemini-2.5-flash has budget
  LLM_MODEL="${COGNEE_LLM_MODEL:-gemini/gemini-2.5-flash}"
  LLM_API_KEY="${GEMINI_API_KEY:?GEMINI_API_KEY not set in env}"
  LLM_RATE_LIMIT_REQUESTS="${LLM_RATE_LIMIT_REQUESTS:-8}"   # gentle: avoid per-min 429s
elif [ "$LLM_BACKEND" = "openrouter" ]; then
  # key has PAID credits -> reliable, no free-pool 429s (The owner-authorised)
  LLM_PROVIDER=custom
  LLM_MODEL="${COGNEE_LLM_MODEL:-openrouter/google/gemini-2.5-flash}"
  LLM_ENDPOINT=https://openrouter.ai/api/v1
  LLM_API_KEY="${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set in env}"
  LLM_RATE_LIMIT_REQUESTS="${LLM_RATE_LIMIT_REQUESTS:-30}"
  # cap max_tokens: cognee defaults to 65535 -> OpenRouter credit-reservation error
  LLM_MAX_COMPLETION_TOKENS="${LLM_MAX_COMPLETION_TOKENS:-8192}"
else
  LLM_PROVIDER=ollama
  LLM_MODEL="${COGNEE_LLM_MODEL:-qwen2.5:7b}"
  LLM_ENDPOINT="$OLLAMA_BASE/v1"
  LLM_API_KEY=ollama
  LLM_RATE_LIMIT_REQUESTS="${LLM_RATE_LIMIT_REQUESTS:-60}"
fi
# --- cognee: embeddings = nomic-768 via ollama (match the memory spine) ------
# EMBED_BACKEND=fastembed (default) — local ONNX, robust to empty/huge inputs
#   (the ollama-nomic path threw persistent 422s on cognee's data points). 384-dim.
# EMBED_BACKEND=ollama — nomic-768 (memory-spine space); fragile here.
EMBED_BACKEND="${EMBED_BACKEND:-fastembed}"
if [ "$EMBED_BACKEND" = "fastembed" ]; then
  EMBEDDING_PROVIDER=fastembed
  EMBEDDING_MODEL="${COGNEE_EMBED_MODEL:-BAAI/bge-small-en-v1.5}"
  EMBEDDING_DIMENSIONS=384
else
  EMBEDDING_PROVIDER=ollama
  EMBEDDING_MODEL=nomic-embed-text:latest
  EMBEDDING_ENDPOINT="$OLLAMA_BASE/api/embed"
  EMBEDDING_DIMENSIONS=768
  HUGGINGFACE_TOKENIZER=nomic-ai/nomic-embed-text-v1.5
  EMBEDDING_MAX_COMPLETION_TOKENS="${EMBEDDING_MAX_COMPLETION_TOKENS:-1024}"
  EMBEDDING_BATCH_SIZE="${EMBEDDING_BATCH_SIZE:-8}"
fi
# --- cognee: relational + vector = pgvector on db-host cognee_bakeoff ----------
DB_PROVIDER=postgres
DB_HOST="$BAKEOFF_PGHOST"
DB_PORT="$BAKEOFF_PGPORT"
DB_USERNAME="$BAKEOFF_PGUSER"
DB_PASSWORD="$BAKEOFF_PGPASSWORD"
DB_NAME="$BAKEOFF_DB_COGNEE"
VECTOR_DB_PROVIDER=pgvector
# --- cognee: graph = embedded kuzu (file, under scratch) --------------------
GRAPH_DATABASE_PROVIDER=kuzu
# --- cognee: storage roots -> scratch (never the clone) ---------------------
DATA_ROOT_DIRECTORY="$HERE/cognee_data"
SYSTEM_ROOT_DIRECTORY="$HERE/cognee_system"
# --- cognee: single-user (no auth / no per-tenant DBs) ----------------------
ENABLE_BACKEND_ACCESS_CONTROL=False
REQUIRE_AUTHENTICATION=False
# --- quiet ------------------------------------------------------------------
ENV=local
TELEMETRY_DISABLED=1
LITELLM_LOG=ERROR
TOKENIZERS_PARALLELISM=false
set +a
exec "$@"
