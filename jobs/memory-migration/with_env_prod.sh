#!/usr/bin/env bash
# Sources central secrets + exports cognee PROD config, then execs argv.
# Keeps every credential in-process only — never in argv, stdout or a repo file.
#
#   ./with_env_prod.sh ./venv/bin/python smoke_prod.py
#
# Embedding host: MUST be worker (or db-host). NOT laptop.
# Same model, same digest, DIFFERENT vectors: laptop runs ollama 0.31.1, worker 0.16.1,
# and the newer build pools nomic-embed-text differently (cos 0.826 between them).
# Production embeds via worker, so worker's space is the production space — a stored
# memory.entries vector re-embeds at cos 1.000000 on worker and 0.869 on laptop.
# check_embed_space.py enforces this; run it before any backfill.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${MIMIR_MEMORY_STATE:-$HERE/.state}"
mkdir -p "$STATE"

set -a
# shellcheck disable=SC1091
# laptop uses the central secrets file; db-host gets a scoped one holding only
# OPENROUTER_API_KEY + COGNEE_PGPASSWORD (chmod 600, never in the repo).
source "${MIMIR_SECRETS_FILE:-$HOME/.config/claude-mcp-secrets.env}"

# --- cognee: LLM = OpenRouter gemini-2.5-flash-lite --------------------------
# Metered, not guessed: cognee's ECL makes ~1.3 LLM calls per ~1.2 KB chunk, and
# the token mix is completion-dominated (110k out vs 80k in on a 40-doc sample).
# flash-lite is $0.10/$0.40 per M vs flash's $0.30/$2.50 => ~5.7x cheaper here:
# the 25 811-chunk corpus costs ~$29 instead of ~$164.
#
# There is NO viable free path for a corpus this size: Gemini's free tier caps at
# 20 requests/day/model (~15 chunks/day => centuries), worker's qwen2.5:7b is
# CPU-only at >2 min/call, and OpenRouter's free pool 429s under sustained load.
# Free models are worth using for one-off probes, not for the backfill.
# Backend switch. `claude-shim` routes cognify through the Claude Code subscription via
# the OpenAI-compatible shim on worker (claude_shim.py) — flat-rate, fast, no per-token
# cost, needs an AUTHENTICATED claude on worker. `openrouter` is the paid fallback.
MIMIR_LLM_BACKEND="${MIMIR_LLM_BACKEND:-openrouter}"
if [ "$MIMIR_LLM_BACKEND" = "claude-shim" ]; then
  LLM_PROVIDER=custom
  LLM_MODEL="${COGNEE_LLM_MODEL:-openai/claude-cli}"
  LLM_ENDPOINT="${CLAUDE_SHIM_ENDPOINT:-http://127.0.0.1:8088/v1}"
  LLM_API_KEY="shim-local"          # litellm requires a key; the shim ignores it
  LLM_MAX_COMPLETION_TOKENS="${LLM_MAX_COMPLETION_TOKENS:-8192}"
else
  LLM_PROVIDER=custom
  # FREE tier only (owner's standing rule). Of the 9 free models advertising
  # structured_outputs, only gemma-4-26b actually returns parseable JSON for
  # cognee's extraction schema -- nemotron-120b and gpt-oss-20b both emit BAD
  # JSON, qwen3-next 429s. Measured 2026-07-12. Re-test before switching.
  # Paid flash-lite (~$29 full run) stays one env var away: COGNEE_LLM_MODEL=...
  LLM_MODEL="${COGNEE_LLM_MODEL:-openrouter/google/gemma-4-26b-a4b-it:free}"
  LLM_ENDPOINT=https://openrouter.ai/api/v1
  LLM_API_KEY="${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set}"
  # cognee defaults max_tokens to 65535 -> OpenRouter "requires more credits" 402
  LLM_MAX_COMPLETION_TOKENS="${LLM_MAX_COMPLETION_TOKENS:-8192}"
  # THE CAP MUST GO THROUGH llm_args, NOT LLM_MAX_COMPLETION_TOKENS.
  # cognee's structured-output call (generic_llm_api/adapter.py:186) passes only
  # **merged_kwargs -- it never forwards max_completion_tokens. So the cap was
  # silently dropped, OpenRouter fell back to the model's default max output
  # (gemma-4-26b = 65535), and every request 429'd:
  #   "requires more credits, or fewer max_tokens. You requested up to 65535"
  # Verified: max_tokens=65535 -> 429; max_tokens=8192 -> OK.
  # merged_kwargs = {**self.llm_args, **kwargs}, and llm_args is env-configurable
  # (config.py:84), so this reaches the wire without patching vendored cognee.
  LLM_ARGS="${LLM_ARGS:-{\"max_tokens\": 8192\}}"
fi
LLM_RATE_LIMIT_ENABLED=true
LLM_RATE_LIMIT_INTERVAL=60
# Backend-conditional. This used to be a flat 12/min for BOTH backends — an
# OpenRouter-flash-lite tuning that silently strangled the claude-shim path:
# measured 1,265 LLM calls in 125 min (~10/min, pinned to the cap), projecting
# ~224h for the backfill. Nothing was rate-limiting us but ourselves.
#
# On claude-shim the REAL governor is the shim's own CLAUDE_SHIM_CONCURRENCY
# (it serialises through a thread pool), so cognee's limiter just needs headroom
# above it. If genuine Anthropic 429s appear, lower the shim's concurrency —
# do NOT re-throttle here, or you lose the ability to tell the two apart.
if [ "$MIMIR_LLM_BACKEND" = "claude-shim" ]; then
  # MATCH THE SHIM'S REAL CAPACITY -- do not "un-throttle" this.
  # The shim runs CLAUDE_SHIM_CONCURRENCY workers, each spawning a Claude Code
  # (Node) process; a real cognify call is ~5s. So capacity ~= concurrency*12/min.
  # Set it to 240 and cognee floods the shim: requests sit in the thread-pool queue
  # past litellm's client timeout, the client disconnects, and the shim dies writing
  # to a closed socket (BrokenPipe -> "OpenrouterException - Server disconnected").
  # Measured: 141 APIErrors, zero completed extractions. A queue collapse, not slowness.
  LLM_RATE_LIMIT_REQUESTS="${LLM_RATE_LIMIT_REQUESTS:-30}"
else
  LLM_RATE_LIMIT_REQUESTS="${LLM_RATE_LIMIT_REQUESTS:-18}"  # free tier is 20/min hard
fi
COGNEE_SKIP_CONNECTION_TEST=true

# --- cognee: embeddings = nomic-768, THE one spine space --------------------
# Viable only with nomic_engine.install() (see README stage 0).
# db-host-local (localhost). Was worker (localhost / tailnet localhost) — moved
# 2026-07-11 because worker sleeps and took the whole backfill down with it.
# VERIFIED, not assumed: check_embed_space.py -> cos=1.000000 vs production's
# stored vectors, despite db-host running ollama 0.13.5 and worker 0.16.1.
OLLAMA_EMBED_HOST="${OLLAMA_EMBED_HOST:-http://localhost:11434}"
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text:latest
EMBEDDING_ENDPOINT="$OLLAMA_EMBED_HOST/api/embed"
EMBEDDING_DIMENSIONS=768
HUGGINGFACE_TOKENIZER=nomic-ai/nomic-embed-text-v1.5
EMBEDDING_MAX_COMPLETION_TOKENS="${EMBEDDING_MAX_COMPLETION_TOKENS:-1024}"
EMBEDDING_BATCH_SIZE="${EMBEDDING_BATCH_SIZE:-16}"

# --- cognee: relational + vector = pgvector on db-host `cognee_prod` ----------
DB_PROVIDER=postgres
DB_HOST="${COGNEE_PGHOST:-localhost}"
DB_PORT="${COGNEE_PGPORT:-5432}"
DB_USERNAME="${COGNEE_PGUSER:-cognee}"
DB_PASSWORD="${COGNEE_PGPASSWORD:?COGNEE_PGPASSWORD not set}"
DB_NAME="${COGNEE_DB:-cognee_prod}"
VECTOR_DB_PROVIDER=pgvector
# In access-control mode cognee resolves a per-dataset vector DB and does NOT
# inherit the relational DB_* settings — without these it dials localhost:1234
# (vector/config.py:29). Estate isolation depends on these being right.
VECTOR_DB_HOST="$DB_HOST"
VECTOR_DB_PORT="$DB_PORT"
VECTOR_DB_USERNAME="$DB_USERNAME"
VECTOR_DB_PASSWORD="$DB_PASSWORD"
VECTOR_DB_NAME="$DB_NAME"
VECTOR_DB_URL="$DB_HOST:$DB_PORT"

# --- cognee: graph = embedded kuzu (file) -----------------------------------
GRAPH_DATABASE_PROVIDER=kuzu

# --- cognee: storage roots (never inside the repo) --------------------------
DATA_ROOT_DIRECTORY="$STATE/cognee_data"
SYSTEM_ROOT_DIRECTORY="$STATE/cognee_system"

# --- cognee: estate isolation ----------------------------------------------
# ENABLE_BACKEND_ACCESS_CONTROL=True is what makes `datasets=[...]` actually
# scope retrieval: cognee then gives each dataset its OWN vector + graph database
# context (search.py:317 vs the `else` branch, which searches one global store and
# ignores the dataset argument entirely). pgvector and kuzu are both on cognee's
# multi-user support lists. With it False, work leaks into personal recall —
# measured, see probe_estate_leak.py.
ENABLE_BACKEND_ACCESS_CONTROL="${ENABLE_BACKEND_ACCESS_CONTROL:-True}"
REQUIRE_AUTHENTICATION=False

# --- quiet ------------------------------------------------------------------
ENV=local
TELEMETRY_DISABLED=1
LITELLM_LOG=ERROR
TOKENIZERS_PARALLELISM=false
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
set +a

exec "$@"
