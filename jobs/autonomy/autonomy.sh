#!/usr/bin/env bash
# Mimir autonomy loop wrapper — sources the shared timer env, dispatches to the
# right tool. Sibling of ~/bin/cracks-brief.sh. Deployed to db-host:~/bin/.
#
# Env (~/.config/weekly-review.env): TELEGRAM_SHIM_URL, TW_API_KEY. The tools read
# these from the environment; this wrapper sources the shared file (plain
# NAME=value, so `set -a` is required to export them) and execs the tool.
#
# Subcommands:
#   producer | digest | executor          (the timed jobs)
#   approve | reject | snooze <short-id>   (the verdict CLI)
#   revert <short-id|uuid>                 (undo an executed proposal)
set -u
set -a
[ -f "$HOME/.config/weekly-review.env" ] && . "$HOME/.config/weekly-review.env"
set +a

DIR="$HOME/mimir/autonomy"
PY=/usr/bin/python3
sub="${1:-}"
shift || true

case "$sub" in
  producer) exec "$PY" "$DIR/producer_task_triage.py" "$@" ;;
  digest)   exec "$PY" "$DIR/digest.py" "$@" ;;
  executor) exec "$PY" "$DIR/executor_task_triage.py" "$@" ;;
  revert)   exec "$PY" "$DIR/executor_task_triage.py" --revert "$@" ;;
  show)     exec "$PY" "$DIR/verdict.py" show "$@" ;;
  approve|reject|snooze) exec "$PY" "$DIR/verdict.py" "$sub" "$@" ;;
  *)
    echo "usage: autonomy.sh {producer|digest|executor|show|approve|reject|snooze|revert} [args]" >&2
    echo "       verdicts take a <short-id> or 'all'; add --source NAME to scope" >&2
    exit 2 ;;
esac
