#!/usr/bin/env bash
# Backfill runner, to be executed ON db-host (~/mimir-memory).
#
# db-host is the right host: the job is ~35 h and laptop roams off the home LAN.
# Everything below is on-LAN and always-on.
#
#   ./run_on_db_host.sh check          # preconditions only, writes nothing
#   ./run_on_db_host.sh pilot          # 300-unit sample, spend-capped
#   ./run_on_db_host.sh full           # the whole corpus, resumable
#   ./run_on_db_host.sh status         # progress + spend so far
#
# Resumable: `.state/backfilled.jsonl` is an append-only fsync'd checkpoint, so a
# kill / reboot / 429 storm costs at most the batch in flight.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export MIMIR_SECRETS_FILE="$HOME/.config/mimir-memory.env"
export MIMIR_MEMORY_STATE="$PWD/.state"
# worker over the LAN: it is production's embedding space (check_embed_space.py).
export OLLAMA_EMBED_HOST="${OLLAMA_EMBED_HOST:-http://localhost:11434}"
# db-host-db is on this host
export COGNEE_PGHOST="${COGNEE_PGHOST:-localhost}"
export ENABLE_BACKEND_ACCESS_CONTROL=True
export COGNEE_LLM_MODEL="${COGNEE_LLM_MODEL:-openrouter/google/gemini-2.5-flash-lite}"
# The owner ruled (2026-07-10): normal `work` cognifies on OpenRouter-paid flash-lite, same
# as personal. Client material is confidential -> pointer-only, so no NDA content reaches
# any LLM regardless. This only enables the non-client work slice.
export MIMIR_WORK_PROVIDER_APPROVED="${MIMIR_WORK_PROVIDER_APPROVED:-true}"
# claude-shim (Claude subscription) is the backend — OpenRouter is out of credits.
# Endpoint is the shim on DB_HOST (127.0.0.1:8088). It used to be worker, described here
# as "an always-on Linux host" — it is not. worker is a LAPTOP THAT SLEEPS, and both
# cognee endpoints (shim + ollama) lived on it. When it napped mid-run every doc failed
# Errno 101, the run skipped all 5,451, exited 0, and the migration silently sat at
# 25/5451 for a day. Both halves now run on db-host, so cognee never leaves localhost.
# Embeddings were safe to move: check_embed_space.py measured db-host vs production's
# stored vectors at cos=1.000000 (laptop is the incompatible one, at 0.826) — its
# permanent home (laptop was a laptop stopgap while worker's claude was unauthed).
# openrouter FREE models. claude-shim is RETIRED as a bulk backend: Claude Code
# refused cognee's extraction prompts outright --
#   "API Error: Claude Code is unable to respond to this request, which appears
#    to violate our Usage Policy"
# A Claude Code subscription is not an API for 33k automated extraction calls, and
# no amount of concurrency/rate tuning changes that. Do not re-point this at the shim.
export MIMIR_LLM_BACKEND="${MIMIR_LLM_BACKEND:-openrouter}"
export CLAUDE_SHIM_ENDPOINT="${CLAUDE_SHIM_ENDPOINT:-http://127.0.0.1:8088/v1}"

LOG="$PWD/.state/backfill.log"

wait_for_net() {
  # After a power-loss reboot the host's routes / tailnet take a few seconds to come up.
  # Launching before then makes every cognify fail with "Network is unreachable" — which
  # once quarantined 4,456 good docs in seconds. Refuse to start until the embedding
  # endpoint, the DB, and (when it is the backend) the claude shim are all reachable.
  local db_hp="${COGNEE_PGHOST:-localhost}:${COGNEE_PGPORT:-5432}"
  local oll_hp; oll_hp=$(printf '%s' "$OLLAMA_EMBED_HOST" | sed -E 's#^https?://##; s#/.*##')
  local targets="$oll_hp $db_hp"
  if [ "${MIMIR_LLM_BACKEND:-claude-shim}" = "claude-shim" ]; then
    local shim_hp; shim_hp=$(printf '%s' "$CLAUDE_SHIM_ENDPOINT" | sed -E 's#^https?://##; s#/.*##')
    targets="$shim_hp $targets"
  fi
  local i hp
  for i in $(seq 1 40); do
    local all=1
    for hp in $targets; do
      (exec 3<>"/dev/tcp/${hp%%:*}/${hp##*:}") 2>/dev/null || all=0
    done
    [ "$all" = 1 ] && { echo "preflight ok: $targets"; return 0; }
    echo "preflight: waiting for network ($i/40): $targets" >&2
    sleep 3
  done
  echo "preflight: endpoints unreachable after 120s ($targets) — refusing to start" >&2
  return 1
}

case "${1:-check}" in
  check)
    ./with_env_prod.sh ./venv/bin/python check_embed_space.py
    ;;
  pilot)
    wait_for_net || exit 1
    ./with_env_prod.sh ./venv/bin/python backfill.py --limit 300 --max-mb 3.0 --batch-size 25
    ;;
  full)
    wait_for_net || exit 1
    # setsid+nohup so it survives the ssh session that launched it. The output is piped
    # through a redactor because cognee/alembic logs the DB connection string —
    # password and all — at startup; it must never land in the logfile.
    setsid nohup bash -c '''./with_env_prod.sh ./venv/bin/python backfill.py --batch-size 25 \
        --max-usd "${BACKFILL_MAX_USD:-45}" 2>&1 \
      | sed -u -E "s#(cognee|postgres):[^@ ]+@#\1:REDACTED@#g" >> "'''"$LOG"'''"''' < /dev/null &
    echo "backfill started, pid $!  -> $LOG"
    ;;
  service)
    # Foreground run for systemd supervision (mimir-backfill.service). Same preflight gate
    # and log redaction as `full`, but NOT backgrounded — systemd owns the process, so a
    # reboot auto-resumes it from the checkpoint instead of needing a manual relaunch. tee
    # keeps the redacted output in both the logfile and the journal. pipefail (set at top)
    # propagates backfill.py's exit code through the pipe so the unit's SuccessExitStatus
    # can treat the budget-cap stop (exit 3) as clean.
    wait_for_net || exit 75   # EX_TEMPFAIL: backend not up yet. Listed in the unit's
                             # SuccessExitStatus so it is a QUIET, patient retry (Restart=always)
                             # and does NOT page. Only a mid-run BackendDown (exit 4) or a real
                             # crash pages. Otherwise a sleeping worker alerts every 2.5 min.
    ./with_env_prod.sh ./venv/bin/python backfill.py --batch-size 25 \
        --max-usd "${BACKFILL_MAX_USD:-45}" 2>&1 \
      | sed -u -E "s#(cognee|postgres):[^@ ]+@#\1:REDACTED@#g" | tee -a "$LOG"
    ;;
  shadow)
    ./with_env_prod.sh ./venv/bin/python shadow_tail.py "${@:2}"
    ;;
  status)
    echo -n "units done: "; wc -l < .state/backfilled.jsonl 2>/dev/null || echo 0
    echo -n "corpus:     "; wc -l < .state/corpus.jsonl
    echo -n "quarantined:"; wc -l < .state/quarantine.jsonl 2>/dev/null || echo 0
    echo "--- last progress ---"
    grep -a 'docs  ' "$LOG" 2>/dev/null | tail -3 || echo "(no log yet)"
    echo -n "running: "; pgrep -f backfill.py >/dev/null && echo yes || echo no
    ;;
  *)
    echo "usage: $0 {check|pilot|full|shadow|status}" >&2; exit 2;;
esac
