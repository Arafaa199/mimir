#!/usr/bin/env python3
"""Autonomy producer — task_triage (spec 09 §7.1, step 2). DETERMINISTIC, no LLM.

Reads captured items in ops.task_queue (status='pending', source in plaud/claude)
and emits PROPOSAL rows (new task_queue rows, status='proposed', domain='task_triage'):

  * route_to_tw  — up to N/day, newest-first, for candidates surviving the
                   route_gating precision filters (config): assignee ∈ owner set,
                   idle <= route_max_idle_days, item_type ∈ task set, action text
                   non-vague, not already an open TW task. Subject-domain maps to a
                   TW project. params carry target_task_id + tw_project + priority +
                   provenance + assignee.
  * bulk_archive_stale — exactly ONE proposal (source_id 'bulk_archive_stale:v1')
                   covering the ancient backlog (idle > stale_idle_days): a list of
                   ids + count + a snapshot for revert, so the swamp drains on one
                   owner decision instead of poisoning weeks of digests.

The route_gating filters (autonomy-config.json, added 2026-07-25) are the precision
fix: the captured pool is ~50% assignee=other and dominated by stale work-meeting
action items, so routing every fresh subject-mapped row produced a ~0%-precision
digest. Gating is DETERMINISTIC and only NARROWS candidates — it never touches the
gate, needs_human, tiers, trust, or bulk. Absent config ⇒ old behaviour (no-op).

Idempotency = the existing unique (source, source_id) index. A rejected proposal's
row keeps its source_id, so its target is never re-proposed. For every emitted row
the gate ops.can_auto_execute(...) is consulted and its verdict recorded as a
decision='proposed' action_audit row (audit-first). Expect can_auto=false for all:
kill switch OFF, task_triage=propose at 0% trust — NOTHING auto-executes.

Usage: producer_task_triage.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys

import common
from common import log, psql, psql_scalar, psql_json, sql_str, sql_jsonb

TAG = "autonomy-producer"
VALID_TIERS = {"auto", "draft", "block", "info"}


def _domain_in_list(cfg: dict) -> str:
    keys = sorted(cfg["subject_to_tw_project"].keys())
    return ", ".join(sql_str(k) for k in keys)


def today_route_count(cfg: dict) -> int:
    src = sql_str(cfg["producer_source"])
    out = psql_scalar(
        f"SELECT count(*) FROM ops.task_queue "
        f"WHERE source = {src} AND extracted_action = 'route_to_tw' "
        f"AND created_at >= date_trunc('day', now());")
    return int(out or "0")


# --------------------------------------------------------------------------- #
# route_gating — deterministic precision filters (config-driven, no LLM).
# Absent/empty config resolves to old behaviour: a no-op that narrows nothing.
# --------------------------------------------------------------------------- #
def route_gating(cfg: dict) -> dict:
    g = dict(cfg.get("route_gating") or {})
    g.setdefault("route_max_idle_days", int(cfg.get("stale_idle_days", 60)))
    g.setdefault("assignees", [])          # [] => no assignee filter (old behaviour)
    g.setdefault("item_types", [])         # [] => no item_type filter
    g.setdefault("min_action_chars", 0)
    g.setdefault("exclude_action_regexes", [])
    g.setdefault("tw_dedup", {})
    return g


def _norm_desc(s: str | None) -> str:
    """Normalise a task description for exact dedup: lower, strip punctuation,
    collapse whitespace. Conservative — only an exact normalised match dedups."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tw_open_norm(cfg: dict) -> set[str] | None:
    """Normalised descriptions of OPEN tw-api tasks. None => unavailable (caller
    fail-opens: never suppress on an unreachable dedup source)."""
    try:
        url = cfg["tw_api_url"].rstrip("/") + "/tasks?filter=status:pending"
        status, body = common.http_json(
            "GET", url, None, {"X-TW-Key": common.TW_API_KEY},
            timeout=12, retries=2, backoff=2.0)
    except Exception:
        return None
    if not (200 <= status < 300):
        return None
    tasks = body.get("tasks") or []
    return {_norm_desc(t.get("description")) for t in tasks if t.get("description")}


def _apply_text_filter(g: dict, cands: list[dict], verbose: bool) -> list[dict]:
    pats = [re.compile(p, re.IGNORECASE) for p in g["exclude_action_regexes"]]
    if not pats:
        return cands
    kept = []
    for c in cands:
        txt = c.get("action_text") or ""
        if any(p.search(txt) for p in pats):
            if verbose:
                log(TAG, f"  drop(vague)   {c['id'][:8]} :: {txt[:60]}")
            continue
        kept.append(c)
    return kept


def _apply_tw_dedup(cfg: dict, g: dict, cands: list[dict], verbose: bool) -> list[dict]:
    td = g.get("tw_dedup") or {}
    if not td.get("enabled") or not cands:
        return cands
    open_norm = _tw_open_norm(cfg)
    if open_norm is None:                       # tw-api unreachable
        if td.get("fail_open", True):
            log(TAG, "tw_dedup: tw-api unavailable -> fail-open (no dedup this run)")
            return cands
        log(TAG, "tw_dedup: tw-api unavailable & fail_open=false -> dropping routes")
        return []
    kept = []
    for c in cands:
        n = _norm_desc(c.get("action_text"))
        if n and n in open_norm:
            if verbose:
                log(TAG, f"  drop(tw-dup)  {c['id'][:8]} :: {(c.get('action_text') or '')[:60]}")
            continue
        kept.append(c)
    return kept


def fetch_route_candidates(cfg: dict, budget: int, verbose: bool = False) -> list[dict]:
    if budget <= 0:
        return []
    g = route_gating(cfg)
    idle = int(g["route_max_idle_days"])
    domains = _domain_in_list(cfg)
    src_list = ", ".join(sql_str(s) for s in cfg["sources"])
    prod = sql_str(cfg["producer_source"])

    clauses = [
        "t.status='pending'",
        f"t.source IN ({src_list})",
        f"t.domain IN ({domains})",
        f"t.updated_at >= now() - interval '{idle} days'",
    ]
    if g["assignees"]:
        alist = ", ".join(sql_str(a.lower()) for a in g["assignees"])
        clauses.append(f"lower(COALESCE(t.raw_content->>'assignee','')) IN ({alist})")
    if g["item_types"]:
        ilist = ", ".join(sql_str(i) for i in g["item_types"])
        clauses.append(f"COALESCE(t.raw_content->>'item_type','task') IN ({ilist})")
    if int(g["min_action_chars"]) > 0:
        clauses.append(
            f"length(COALESCE(t.extracted_action,'')) >= {int(g['min_action_chars'])}")
    clauses.append(
        f"NOT EXISTS (SELECT 1 FROM ops.task_queue p WHERE p.source={prod} "
        "AND p.source_id = 'route_to_tw:'||t.id::text)")
    where = " AND ".join(clauses)

    # Over-fetch so the Python-side text/dedup filters can drop some and still fill
    # budget; the column filters already shrink this to a handful.
    limit = budget + 25
    sql = (
        "SELECT COALESCE(jsonb_agg(c ORDER BY c_created DESC), '[]')::text FROM ("
        "  SELECT jsonb_build_object("
        "    'id', t.id::text,"
        "    'tier', t.tier,"
        "    'priority', t.priority,"
        "    'tc', t.triage_confidence::text,"
        "    'subject', t.domain,"
        "    'action_text', t.extracted_action,"
        "    'assignee', t.raw_content->>'assignee',"
        "    'item_type', t.raw_content->>'item_type',"
        "    'source', t.source"
        "  ) AS c, t.created_at AS c_created"
        "  FROM ops.task_queue t"
        f"  WHERE {where}"
        "  ORDER BY t.created_at DESC"
        f"  LIMIT {limit}"
        ") sub;"
    )
    cands = psql_json(sql) or []
    cands = _apply_text_filter(g, cands, verbose)
    cands = _apply_tw_dedup(cfg, g, cands, verbose)
    return cands[:budget]


def fetch_stale(cfg: dict) -> dict:
    stale = int(cfg["stale_idle_days"])
    src_list = ", ".join(sql_str(s) for s in cfg["sources"])
    sql = (
        "SELECT jsonb_build_object("
        "  'count', count(*),"
        "  'snapshot', COALESCE(jsonb_agg(jsonb_build_object("
        "     'id', id::text, 'status', status) ORDER BY updated_at ASC), '[]'),"
        "  'target_ids', COALESCE(jsonb_agg(id::text ORDER BY updated_at ASC), '[]')"
        ")::text "
        "FROM ops.task_queue "
        f"WHERE status='pending' AND source IN ({src_list}) "
        f"  AND updated_at < now() - interval '{stale} days';"
    )
    return psql_json(sql) or {"count": 0, "snapshot": [], "target_ids": []}


def bulk_exists(cfg: dict) -> bool:
    src = sql_str(cfg["producer_source"])
    out = psql_scalar(
        f"SELECT count(*) FROM ops.task_queue "
        f"WHERE source = {src} AND source_id = 'bulk_archive_stale:v1';")
    return int(out or "0") > 0


# --------------------------------------------------------------------------- #
# Emit — one atomic statement inserts the proposal AND its audit row; on
# (source,source_id) conflict BOTH are skipped (perfect idempotency).
# --------------------------------------------------------------------------- #
def _emit_cte(cfg: dict, source_id: str, action: str, tier: str, priority: int,
              tc: str, reason: str, raw: dict) -> tuple[int, int]:
    prod = sql_str(cfg["producer_source"])
    sid = sql_str(source_id)
    tier = tier if tier in VALID_TIERS else "block"
    sql = (
        "WITH ins AS ("
        "  INSERT INTO ops.task_queue"
        "    (source, source_id, extracted_action, domain, tier, priority,"
        "     triage_confidence, needs_human, status, triage_reason, raw_content)"
        f"  VALUES ({prod}, {sid}, {sql_str(action)}, 'task_triage', {sql_str(tier)},"
        f"          {int(priority)}, {tc}, true, 'proposed', {sql_str(reason)}, {sql_jsonb(raw)})"
        "  ON CONFLICT (source, source_id) WHERE source_id IS NOT NULL DO NOTHING"
        "  RETURNING id, source_id, extracted_action, domain, tier, triage_confidence, raw_content"
        "), aud AS ("
        "  INSERT INTO ops.action_audit"
        "    (ts, agent, surface, domain, action, params, decision,"
        "     trust_pct_at, tier_at, approver, idempotency_key)"
        "  SELECT now(), 'autonomy-producer', 'producer_task_triage', ins.domain,"
        "    ins.extracted_action,"
        "    jsonb_build_object("
        "      'task_id', ins.id,"
        "      'target_task_id', ins.raw_content->>'target_task_id',"
        "      'tw_project', ins.raw_content->>'tw_project',"
        "      'reversible', (SELECT reversible FROM ops.autonomy_actions WHERE action=ins.extracted_action),"
        "      'provenance', ins.raw_content->>'provenance',"
        "      'can_auto', ops.can_auto_execute("
        "         ins.tier, ins.triage_confidence, ins.domain,"
        "         (SELECT reversible FROM ops.autonomy_actions WHERE action=ins.extracted_action),"
        "         ins.raw_content->>'provenance', NULL, ins.raw_content)"
        "    ),"
        "    'proposed',"
        "    COALESCE((SELECT trust_pct FROM ops.trust_current WHERE domain=ins.domain), 0),"
        "    ops.trust_tier(ins.domain),"
        "    'autonomy-producer',"
        "    'propose:'||ins.source_id"
        "  FROM ins RETURNING 1"
        ") SELECT (SELECT count(*) FROM ins) || '|' || (SELECT count(*) FROM aud);"
    )
    out = psql_scalar(sql)
    ins_n, aud_n = (int(x) for x in out.split("|"))
    return ins_n, aud_n


def emit_route(cfg: dict, cand: dict) -> bool:
    subject = cand["subject"]
    proj = cfg["subject_to_tw_project"].get(subject)
    if not proj:
        return False  # unmapped subject -> not deterministically routable, skip
    tid = cand["id"]
    twp = cfg["tw_priority_by_row_priority"].get(str(cand["priority"]), "")
    raw = {
        "proposed_action": "route_to_tw",
        "target_task_id": tid,
        "tw_project": proj,
        "tw_priority": twp,
        "provenance": cfg["provenance_by_source"].get(cand["source"], "untrusted"),
        "description": cand["action_text"],
        "title": (cand["action_text"] or "")[:90],
        "subject_domain": subject,
        "target_priority": cand["priority"],
        "target_source": cand["source"],
        "assignee": cand.get("assignee"),
    }
    reason = f"autonomy: route {subject} item -> TW/{proj}"
    ins_n, _ = _emit_cte(cfg, f"route_to_tw:{tid}", "route_to_tw", cand["tier"],
                         int(cand["priority"]), cand["tc"], reason, raw)
    return ins_n == 1


def emit_bulk(cfg: dict, stale: dict) -> bool:
    n = int(stale["count"])
    if n <= 0 or bulk_exists(cfg):
        return False
    raw = {
        "proposed_action": "bulk_archive_stale",
        "provenance": "trusted",
        "idle_days_threshold": int(cfg["stale_idle_days"]),
        "count": n,
        "target_ids": stale["target_ids"],
        "snapshot": stale["snapshot"],
    }
    reason = f"autonomy: archive {n} stale (>{cfg['stale_idle_days']}d) captured items"
    ins_n, _ = _emit_cte(cfg, "bulk_archive_stale:v1", "bulk_archive_stale",
                         "block", 3, "1.00", reason, raw)
    return ins_n == 1


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Mimir autonomy producer (task_triage)")
    ap.add_argument("--dry-run", action="store_true",
                    help="assemble + print candidates; write nothing")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = common.load_config(args.config)

    verbose = args.verbose or args.dry_run
    used_today = today_route_count(cfg)
    cap = int(cfg["daily_route_cap"])
    budget = max(0, cap - used_today)
    cands = fetch_route_candidates(cfg, budget, verbose)
    stale = fetch_stale(cfg)

    if verbose:
        g = route_gating(cfg)
        log(TAG, f"route_gating: idle<={g['route_max_idle_days']}d "
                 f"assignees={g['assignees'] or 'ANY'} item_types={g['item_types'] or 'ANY'} "
                 f"min_chars={g['min_action_chars']} "
                 f"vague_patterns={len(g['exclude_action_regexes'])} "
                 f"tw_dedup={'on' if (g['tw_dedup'] or {}).get('enabled') else 'off'}")
        log(TAG, f"cap={cap} used_today={used_today} budget={budget} "
                 f"route_candidates={len(cands)} stale_targets={stale['count']} "
                 f"bulk_exists={bulk_exists(cfg)}")
        for c in cands:
            proj = cfg["subject_to_tw_project"].get(c["subject"], "?")
            log(TAG, f"  route {c['id'][:8]} [{c['subject']}->{proj}] "
                     f"assignee={c.get('assignee')} tier={c['tier']} p{c['priority']} "
                     f":: {(c['action_text'] or '')[:70]}")

    if args.dry_run:
        log(TAG, "dry-run: no rows written")
        return 0

    routed = sum(1 for c in cands if emit_route(cfg, c))
    bulk = emit_bulk(cfg, stale)

    log(TAG, f"done: proposed_routes={routed} bulk_proposal={'1' if bulk else '0'} "
             f"(skipped_existing={len(cands) - routed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
