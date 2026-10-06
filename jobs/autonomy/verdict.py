#!/usr/bin/env python3
"""Autonomy verdict CLI — spec 09 §4.3. Owner inspects + approves/rejects/snoozes.

Runs anywhere with DB access (common.psql falls back to `ssh db-host` off-host, so a
symlink on laptop works).

  show    <short-id>            print the proposal + its full underlying item
  approve <short-id|all>        -> status='approved' + audit 'approved'  (executor runs it)
  reject  <short-id|all>        -> status='rejected' + audit 'denied'    (feeds graduation)
  snooze  <short-id|all>        -> status='snoozed'  (NO audit row — no signal; re-surfaces)

`all` acts on every currently-**proposed** row (snoozed rows are left as-is), each
getting its own audit row; a zero-target `all` is a safe no-op. `--source` scopes
which producer's proposals are addressed (default: the task_triage producer).

Approve does NOT execute; the executor (every 30 min) does, audit-first. Nothing
auto-executes — task_triage is at propose.

Usage:  verdict.py {show|approve|reject|snooze} <short-id|all> [--source NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import common
from common import log, psql, psql_json, sql_str

TAG = "autonomy-verdict"
_DECISION = {"approve": "approved", "reject": "denied"}
_NEWSTATUS = {"approve": "approved", "reject": "rejected", "snooze": "snoozed"}


def approver() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "owner"


# --------------------------------------------------------------------------- #
# Resolve
# --------------------------------------------------------------------------- #
def resolve(source: str, ident: str, open_only: bool = True) -> list[dict]:
    """Resolve one proposal by short id (8 hex) or full uuid."""
    ident = ident.strip().lower()
    match = (f"left(id::text, 8) = {sql_str(ident)}" if len(ident) == 8
             else f"id::text = {sql_str(ident)}")
    status_f = "AND status IN ('proposed','snoozed') " if open_only else ""
    sql = (
        "SELECT COALESCE(jsonb_agg(jsonb_build_object("
        "  'id', id::text, 'source_id', source_id, 'action', extracted_action,"
        "  'status', status, 'title', raw_content->>'title') ORDER BY id), '[]')::text "
        f"FROM ops.task_queue WHERE source = {sql_str(source)} {status_f}AND {match};"
    )
    return psql_json(sql) or []


def resolve_all_proposed(source: str) -> list[dict]:
    """All currently-proposed rows for a source (snoozed excluded from bulk)."""
    sql = (
        "SELECT COALESCE(jsonb_agg(jsonb_build_object("
        "  'id', id::text, 'source_id', source_id, 'action', extracted_action,"
        "  'status', status, 'title', COALESCE(raw_content->>'title',"
        "  raw_content->>'description')) ORDER BY created_at), '[]')::text "
        f"FROM ops.task_queue WHERE source = {sql_str(source)} AND status = 'proposed';"
    )
    return psql_json(sql) or []


# --------------------------------------------------------------------------- #
# Apply a verdict to ONE resolved row (audit-first for approve/reject)
# --------------------------------------------------------------------------- #
def apply_verdict(verb: str, row: dict) -> None:
    full_id = sql_str(row["id"])
    new_status = _NEWSTATUS[verb]

    if verb == "snooze":
        psql(f"UPDATE ops.task_queue SET status='snoozed', updated_at=now() "
             f"WHERE id = {full_id};")
        return

    decision = _DECISION[verb]
    who = sql_str(approver())
    sql = (
        "WITH tgt AS ("
        "  SELECT id, source_id, extracted_action, domain, raw_content"
        f"  FROM ops.task_queue WHERE id = {full_id}"
        "), aud AS ("
        "  INSERT INTO ops.action_audit"
        "    (ts, agent, surface, domain, action, params, decision,"
        "     trust_pct_at, tier_at, approver, idempotency_key)"
        "  SELECT now(), 'autonomy-verdict', 'verdict-cli', t.domain, t.extracted_action,"
        "    jsonb_build_object('task_id', t.id, 'target_task_id',"
        "                       t.raw_content->>'target_task_id', 'via', 'cli'),"
        f"    {sql_str(decision)},"
        "    COALESCE((SELECT trust_pct FROM ops.trust_current WHERE domain=t.domain), 0),"
        "    ops.trust_tier(t.domain),"
        f"    {who}, {sql_str(decision + ':')}||t.source_id"
        "  FROM tgt t RETURNING 1"
        ")"
        f"  UPDATE ops.task_queue SET status = {sql_str(new_status)}, updated_at=now()"
        "  WHERE id = (SELECT id FROM tgt);"
    )
    psql(sql)


# --------------------------------------------------------------------------- #
# show — the "where is the source of this information stored" answer
# --------------------------------------------------------------------------- #
def show(source: str, ident: str) -> int:
    rows = resolve(source, ident, open_only=False)
    if not rows:
        log(TAG, f"no proposal matches '{ident}' for source {source}")
        return 1
    if len(rows) > 1:
        log(TAG, f"ambiguous '{ident}' matches {len(rows)}")
        return 1
    full_id = rows[0]["id"]
    sql = (
        f"WITH p AS (SELECT * FROM ops.task_queue WHERE id = {sql_str(full_id)}) "
        "SELECT jsonb_build_object("
        "  'proposal', (SELECT jsonb_build_object("
        "     'id', id::text, 'short', left(id::text,8), 'action', extracted_action,"
        "     'status', status, 'domain', domain, 'source_id', source_id,"
        "     'created_at', created_at::text, 'executed_at', executed_at::text,"
        "     'reverted_at', reverted_at::text, 'revert_action', revert_action,"
        "     'result', result, 'reason', triage_reason, 'raw', raw_content) FROM p),"
        "  'target', (SELECT jsonb_build_object("
        "     'id', t.id::text, 'short', left(t.id::text,8), 'source', t.source,"
        "     'status', t.status, 'created_at', t.created_at::text,"
        "     'action', t.extracted_action, 'reason', t.triage_reason,"
        "     'raw', t.raw_content)"
        "     FROM ops.task_queue t, p"
        "     WHERE t.id = (p.raw_content->>'target_task_id')::uuid)"
        ")::text;"
    )
    data = psql_json(sql) or {}
    _print_show(data)
    return 0


def _print_show(data: dict) -> None:
    p = data.get("proposal") or {}
    t = data.get("target")
    praw = p.get("raw") or {}
    out = [f"PROPOSAL {p.get('short')}  [{p.get('action')} · {p.get('domain')} "
           f"· status={p.get('status')}]",
           f"  created:     {p.get('created_at')}",
           f"  provenance:  {praw.get('provenance')}",
           f"  reason:      {p.get('reason')}"]
    if p.get("action") == "route_to_tw":
        out.append(f"  proposed:    file into TaskWarrior project "
                   f"'{praw.get('tw_project')}' (priority {praw.get('tw_priority') or '-'})")
    elif p.get("action") == "bulk_archive_stale":
        out.append(f"  proposed:    archive {praw.get('count')} items idle "
                   f">{praw.get('idle_days_threshold')}d")
    out.append(f"  params:      {json.dumps(praw, ensure_ascii=False)}")
    if p.get("executed_at"):
        out.append(f"  executed_at: {p.get('executed_at')}   result: "
                   f"{json.dumps(p.get('result'), ensure_ascii=False)}")
    if p.get("revert_action"):
        rv = p.get("reverted_at")
        out.append(f"  revert:      {p.get('revert_action')}"
                   + (f"   (reverted_at {rv})" if rv else ""))

    out.append("")
    if t:
        traw = t.get("raw") or {}
        out.append(f"TARGET {t.get('short')}  [source={t.get('source')} "
                   f"· status={t.get('status')} · created {t.get('created_at')}]")
        out.append(f"  task:        {t.get('action')}")
        out.append(f"  reason:      {t.get('reason')}")
        out.append("  source of the information (raw_content):")
        for line in json.dumps(traw, ensure_ascii=False, indent=2).split("\n"):
            out.append("    " + line)
    else:
        ids = praw.get("target_ids") or []
        out.append(f"TARGETS: {len(ids)} captured items (bulk). first 10:")
        out.extend("    " + i for i in ids[:10])
    print("\n".join(out))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Mimir autonomy verdict CLI")
    ap.add_argument("verb", choices=["show", "approve", "reject", "snooze"])
    ap.add_argument("target", help="short-id | uuid | 'all'")
    ap.add_argument("--source", default=None, help="producer source scope")
    args = ap.parse_args()

    cfg = common.load_config()
    source = args.source or cfg["producer_source"]

    if args.verb == "show":
        return show(source, args.target)

    if args.target.strip().lower() == "all":
        rows = resolve_all_proposed(source)
        if not rows:
            log(TAG, f"no open (proposed) rows for source {source} — nothing to do")
            return 0
        for r in rows:
            apply_verdict(args.verb, r)
            print(f"  {args.verb:>7} {r['id'][:8]} {r['action']} :: "
                  f"{(r.get('title') or '')[:60]}")
        log(TAG, f"{args.verb} all -> {_NEWSTATUS[args.verb]}: {len(rows)} row(s) "
                 f"(source {source})")
        return 0

    rows = resolve(source, args.target, open_only=True)
    if not rows:
        log(TAG, f"no open proposal matches '{args.target}'")
        return 1
    if len(rows) > 1:
        log(TAG, f"ambiguous '{args.target}' matches {len(rows)}: "
                 + ", ".join(r["id"][:8] for r in rows))
        return 1
    row = rows[0]
    apply_verdict(args.verb, row)
    log(TAG, f"{args.verb} -> {_NEWSTATUS[args.verb]}: {row['id'][:8]} "
             f"{row['action']} :: {(row.get('title') or '')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
