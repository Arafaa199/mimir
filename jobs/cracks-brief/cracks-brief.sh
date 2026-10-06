#!/usr/bin/env bash
# mimir cracks-brief wrapper — source shared timer env, run the Python job.
# Sibling of ~/bin/monthly-money-narrative.sh. Deployed to db-host:~/bin/.
#
# Env (~/.config/weekly-review.env): TELEGRAM_SHIM_URL, TW_API_KEY,
# OPENROUTER_API_KEY (phrasing, optional). The Python job reads these from
# the environment; this wrapper just sources the shared file and execs it.
set -u
# set -a: auto-export every var the env file sets, so the exec'd Python inherits
# them (the file uses plain NAME=value, not `export`, so this is required).
set -a
[ -f "$HOME/.config/weekly-review.env" ] && . "$HOME/.config/weekly-review.env"
set +a
exec /usr/bin/python3 "$HOME/mimir/cracks-brief/cracks_brief.py" "$@"
