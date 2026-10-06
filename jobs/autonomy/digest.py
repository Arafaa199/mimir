#!/usr/bin/env python3
"""Autonomy digest — spec 09 §4.3, step 3. ONE batched Telegram message/day.

Collects every open proposal (status in proposed/snoozed) for the task_triage
producer, groups them (routes vs the bulk-archive), and delivers ONE message via
the Odin shim (the cracks-brief rail). Owner-direct sink only. Each line carries a
short id; the footer explains the verdict CLI. No message when there are zero
proposals. Idempotent per UTC day via a state file (re-run same day = no re-send).

Usage: digest.py [--dry-run] [--force] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import common
from common import log, psql_json, sql_str

TAG = "autonomy-digest"
STATE_DIR = os.path.expanduser("~/.local/state/mimir-autonomy")
TG_LIMIT = 3800  # headroom under Telegram's 4096-char message limit


def _marker_path(day: str) -> str:
    return os.path.join(STATE_DIR, f"digest_sent_{day}")


def fetch_proposals(cfg: dict) -> list[dict]:
    src = sql_str(cfg["producer_source"])
    sql = (
        "SELECT COALESCE(jsonb_agg(jsonb_build_object("
        "  'short', left(id::text, 8),"
        "  'action', extracted_action,"
        "  'status', status,"
        "  'title', raw_content->>'title',"
        "  'description', raw_content->>'description',"
        "  'tw_project', raw_content->>'tw_project',"
        "  'tw_priority', raw_content->>'tw_priority',"
        "  'subject', raw_content->>'subject_domain',"
        "  'count', raw_content->>'count'"
        ") ORDER BY extracted_action, created_at), '[]')::text "
        f"FROM ops.task_queue "
        f"WHERE source = {src} AND status IN ('proposed', 'snoozed');"
    )
    return psql_json(sql) or []


def _fmt_title(p: dict, limit: int = 300) -> str:
    # prefer the FULL description — the stored 'title' is truncated to 90 by the
    # producer, so using it would cut sentences before the owner can judge them.
    # Only fall back to a hard cut past ~300 chars (an outlier); Telegram wraps.
    t = " ".join((p.get("description") or p.get("title") or "").split())
    return (t[:limit - 1] + "…") if len(t) > limit else t


def _chunk(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line boundaries into messages under Telegram's 4096 cap. With a
    ~300-char per-line title cap and ≤~11 proposals this is usually one message;
    a large batch splits into several rather than truncating any item."""
    chunks: list[str] = []
    cur = ""
    for ln in text.split("\n"):
        add = (("\n" if cur else "") + ln)
        if cur and len(cur) + len(add) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _tg_safe(text: str) -> str:
    """The Odin shim posts to Telegram with HTML parse mode, so raw < > & abort
    the send (a proposal title is arbitrary transcript text and may contain any of
    them). Map to visually-identical non-special code points — lossless to read,
    never parsed as markup."""
    return (text.replace("&", "＆")   # ＆ fullwidth ampersand
                .replace("<", "‹")    # ‹ single left guillemet
                .replace(">", "›"))   # › single right guillemet


def format_digest(cfg: dict, proposals: list[dict], day: str) -> str:
    emoji = cfg["digest"]["header_emoji"]
    routes = [p for p in proposals if p["action"] == "route_to_tw"]
    bulk = [p for p in proposals if p["action"] == "bulk_archive_stale"]
    other = [p for p in proposals
             if p["action"] not in ("route_to_tw", "bulk_archive_stale")]

    lines = [f"{emoji} {cfg['digest']['title']} · {day}"]

    if routes:
        lines.append("")
        lines.append(f"➡️ Route to TaskWarrior ({len(routes)}):")
        for p in routes:
            snz = " ⏳" if p["status"] == "snoozed" else ""
            twp = f" [{p['tw_priority']}]" if p.get("tw_priority") else ""
            lines.append(f"• {p['short']} [{p.get('subject')}→{p.get('tw_project')}]"
                         f"{twp}{snz} {_fmt_title(p)}")

    if bulk:
        lines.append("")
        lines.append("🗄 Archive stale backlog:")
        for p in bulk:
            snz = " ⏳" if p["status"] == "snoozed" else ""
            lines.append(f"• {p['short']}{snz} {p.get('count')} items idle "
                         f"over 60d — one decision to drain the swamp")

    for p in other:
        lines.append(f"• {p['short']} {p['action']} {_fmt_title(p)}")

    lines.append("")
    lines.append("Verdict:  autonomy approve|reject|snooze <id>   (or: all)")
    lines.append("Details:  autonomy.sh show <id>")
    lines.append("Nothing auto-executes — task_triage is at propose (0% trust); "
                 "each needs your explicit yes.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mimir autonomy digest")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send or mark")
    ap.add_argument("--force", action="store_true", help="ignore the daily sent-marker")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = common.load_config(args.config)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = _marker_path(day.replace("-", ""))

    proposals = fetch_proposals(cfg)
    if not proposals:
        log(TAG, "0 proposals — no message")
        return 0

    if os.path.exists(marker) and not args.force and not args.dry_run:
        log(TAG, f"already sent today ({day}) — skip (use --force to resend)")
        return 0

    chunks = _chunk(_tg_safe(format_digest(cfg, proposals, day)))
    if args.verbose or args.dry_run:
        for i, c in enumerate(chunks, 1):
            print(f"\n===== message {i}/{len(chunks)} ({len(c)} chars) =====\n{c}\n")

    if args.dry_run:
        log(TAG, f"dry-run: {len(proposals)} proposals in {len(chunks)} message(s), "
                 "not sending")
        return 0

    ok = True
    try:
        for c in chunks:
            if not common.deliver_telegram(c):
                ok = False
                break
    except Exception as e:  # noqa: BLE001 — delivery failure must be visible, not fatal
        log(TAG, f"delivery failed: {e}")
        ok = False

    if ok:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat() + "\n")
        log(TAG, f"delivered {len(proposals)} proposals in {len(chunks)} message(s); "
                 f"marker {os.path.basename(marker)}")
        return 0
    log(TAG, "not delivered")
    return 1


if __name__ == "__main__":
    sys.exit(main())
