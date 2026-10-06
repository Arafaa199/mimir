#!/usr/bin/env bash
# mimir cracks-actions wrapper — source shared timer env, run the long-poll handler.
# Sibling of cracks-brief.sh. Deployed to db-host:~/bin/.
#
# Env (~/.config/weekly-review.env): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (the
# alerts bot + owner id — added for the actionable loop), TW_API_KEY, optional
# TW_API_URL / CRACKS_SNOOZE_DAYS. The Python job reads these from the environment.
set -u
# set -a: auto-export every var the env file sets (file uses plain NAME=value).
set -a
[ -f "$HOME/.config/weekly-review.env" ] && . "$HOME/.config/weekly-review.env"
set +a
exec /usr/bin/python3 "$HOME/mimir/cracks-brief/cracks_actions.py"
