#!/usr/bin/env bash
# mimir-fab wrapper — source env, run the fabrication HTTP service. Deployed to
# db-host:~/bin/. Secrets: ~/.config/weekly-review.env (OPENROUTER_API_KEY,
# TELEGRAM_SHIM_URL) + ~/.config/fab.env (FAB_API_KEY, MESHY_API_KEY).
set -u
set -a
[ -f "$HOME/.config/weekly-review.env" ] && . "$HOME/.config/weekly-review.env"
[ -f "$HOME/.config/fab.env" ]           && . "$HOME/.config/fab.env"
set +a
cd "$HOME/mimir/fab" || exit 1
exec /usr/bin/python3 "$HOME/mimir/fab/server.py"
