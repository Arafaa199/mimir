#!/usr/bin/env python3
"""Autonomy executor — task_triage (spec 09 §4.4/§4.5, step 4). Runs on db-host
every 30 min. Processes ONLY status='approved' proposals; nothing auto-fires.

Closed action allow-list (dispatch by action; unknown => refuse + audit):

  route_to_tw        POST tw-api :8250 (project + description + priority), resolve
                     the created task's uuid, then AUDIT-FIRST finalize (one txn):
                     audit decision='executed' -> proposal completed + result{uuid}
                     + revert_action 'tw_delete:<uuid>;restore_target:<tid>' + the
                     target row marked completed (snapshot_before keeps its prior
                     status). Idempotent: result-presence guard + a per-target TW
                     tag recovers a task a crashed run already created.
  archive_noise      target -> 'skipped' (revert = restore prior status).
  bulk_archive_stale batch of targets -> 'skipped' (revert = restore from snapshot).

Revert (`--revert <id>`): calls ops.revert() (which validates, stamps reverted_at,
writes the 'reverted' audit row and RETURNS the revert_action), then replays that
action here — deletes the TW task and restores the target(s).

Usage:  executor_task_triage.py [--verbose]
        executor_task_triage.py --revert <short-id|uuid> [--by NAME]
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import quote

import common
from common import log, psql, psql_scalar, psql_json, sql_str, sql_jsonb

TAG = "autonomy-executor"
ALLOWED = {"route_to_tw", "archive_noise", "bulk_archive_stale"}


# --------------------------------------------------------------------------- #
# tw-api client (worker :8250) — the same API cracks reads.
# POST returns no id (verbose=nothing), so the created task's uuid is resolved by
# a unique per-target tag. `+` must be %2B in the query (else it decodes to space);
# the `task` CLI 500s under sync-lock, so resolve/delete retry.
# --------------------------------------------------------------------------- #
def _tw_base(cfg: dict) -> str:
    return cfg["tw_api_url"].rstrip("/")


def _tw_headers() -> dict:
    return {"X-TW-Key": common.TW_API_KEY, "Content-Type": "application/json"}


def _tag_for(cfg: dict, target_id: str) -> str:
    return cfg["tw_tag_prefix"] + target_id.replace("-", "")[:8]


def tw_create(cfg: dict, description: str, project: str, priority: str,
              tags: list[str]) -> tuple[int, dict]:
    body = {"description": description, "project": project, "tags": tags}
    if priority:
        body["priority"] = priority
    return common.http_json("POST", _tw_base(cfg) + "/tasks", body,
                            _tw_headers(), timeout=20, retries=3, backoff=2.0)


def tw_resolve_uuid(cfg: dict, tag: str, tries: int = 6) -> str | None:
    url = _tw_base(cfg) + "/tasks?filter=" + quote("+" + tag, safe="")
    for _ in range(tries):
        try:
            status, body = common.http_json("GET", url, None, _tw_headers(),
                                            timeout=20, retries=1)
        except Exception:
            status, body = 0, {}
        if status == 200 and body.get("count") == 1:
            return body["tasks"][0]["uuid"]
        if status == 200 and body.get("count", 0) > 1:
            return None  # ambiguous — never guess which to delete later
        time.sleep(2)
    return None


def tw_delete(cfg: dict, uuid: str, tries: int = 6) -> bool:
    for _ in range(tries):
        try:
            status, _ = common.http_json("DELETE", _tw_base(cfg) + "/tasks/" + uuid,
                                         None, _tw_headers(), timeout=20, retries=1)
        except Exception:
            status = 0
        if status == 200:
            return True
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def fetch_approved(cfg: dict) -> list[dict]:
    prod = sql_str(cfg["producer_source"])
    sql = (
        "SELECT COALESCE(jsonb_agg(jsonb_build_object("
        "  'id', id::text, 'source_id', source_id, 'action', extracted_action,"
        "  'raw', raw_content, 'result', COALESCE(result, '{}'::jsonb)"
        ") ORDER BY updated_at ASC), '[]')::text "
        f"FROM ops.task_queue WHERE source = {prod} AND status = 'approved';"
    )
    return psql_json(sql) or []


def _target_status(target_id: str) -> str:
    return psql_scalar(
        f"SELECT COALESCE(status,'') FROM ops.task_queue WHERE id = {sql_str(target_id)};")


def _set_error(row_id: str, msg: str) -> None:
    psql(f"UPDATE ops.task_queue SET error = {sql_str(msg[:500])}, updated_at=now() "
         f"WHERE id = {sql_str(row_id)};")


def _audit_and_finalize(cfg: dict, row: dict, result: dict, revert_action: str,
                        snapshot: dict, target_updates_sql: str) -> None:
    """One transaction: audit decision='executed' FIRST, then mutate proposal +
    target(s). The external effect (if any) already happened and was verified."""
    rid = sql_str(row["id"])
    sid_key = sql_str("execute:" + (row["source_id"] or row["id"]))
    sql = (
        "WITH t AS (SELECT id, domain, extracted_action, raw_content"
        f"           FROM ops.task_queue WHERE id = {rid}),"
        " aud AS ("
        "  INSERT INTO ops.action_audit"
        "    (ts, agent, surface, domain, action, params, decision,"
        "     trust_pct_at, tier_at, approver, idempotency_key)"
        "  SELECT now(), 'autonomy-executor', 'executor_task_triage', t.domain,"
        "    t.extracted_action,"
        "    jsonb_build_object('task_id', t.id, 'target_task_id',"
        "        t.raw_content->>'target_task_id', 'result', " + sql_jsonb(result) + "),"
        "    'executed',"
        "    COALESCE((SELECT trust_pct FROM ops.trust_current WHERE domain=t.domain),0),"
        "    ops.trust_tier(t.domain), 'autonomy-executor', " + sid_key +
        "  FROM t RETURNING 1"
        "), prop AS ("
        "  UPDATE ops.task_queue SET status='completed', executed_by='autonomy-executor',"
        f"    executed_at=now(), result={sql_jsonb(result)},"
        f"    snapshot_before={sql_jsonb(snapshot)}, revert_action={sql_str(revert_action)},"
        f"    error=NULL, updated_at=now() WHERE id = {rid} RETURNING 1"
        ")"
        + target_updates_sql
    )
    psql(sql)


def execute_route(cfg: dict, row: dict) -> str:
    raw, result = row["raw"], row["result"]
    if result.get("tw_uuid"):
        return "already-done"
    tid = raw.get("target_task_id")
    proj = raw.get("tw_project")
    desc = raw.get("description") or raw.get("title") or "(captured task)"
    twp = raw.get("tw_priority") or ""
    if not tid or not proj:
        _set_error(row["id"], "route_to_tw missing target_task_id/tw_project")
        return "error"

    tag = _tag_for(cfg, tid)
    uuid = tw_resolve_uuid(cfg, tag)          # recover a task a prior run created
    if uuid is None:
        status, body = tw_create(cfg, desc, proj, twp, ["mimir", tag])
        if not (200 <= status < 300):
            _set_error(row["id"], f"tw create failed: {status} {body}")
            return "error"
        uuid = tw_resolve_uuid(cfg, tag)
    if uuid is None:
        _set_error(row["id"], "tw uuid unresolved after create (retry next run)")
        return "retry"

    prior = _target_status(tid)
    snapshot = {"target_task_id": tid, "target_prior_status": prior or None}
    result_obj = {"tw_uuid": uuid, "tw_project": proj, "tw_tag": tag}
    revert_action = f"tw_delete:{uuid};restore_target:{tid}"
    target_sql = (
        f", tgtupd AS (UPDATE ops.task_queue SET status='completed', updated_at=now()"
        f"   WHERE id = {sql_str(tid)} AND status <> 'completed' RETURNING 1)"
        " SELECT 1;" if prior else " SELECT 1;"
    )
    _audit_and_finalize(cfg, row, result_obj, revert_action, snapshot, target_sql)
    return "executed"


def execute_archive_noise(cfg: dict, row: dict) -> str:
    raw, result = row["raw"], row["result"]
    if result.get("archived"):
        return "already-done"
    tid = raw.get("target_task_id")
    if not tid:
        _set_error(row["id"], "archive_noise missing target_task_id")
        return "error"
    prior = _target_status(tid)
    snapshot = {"target_task_id": tid, "target_prior_status": prior or None}
    result_obj = {"archived": tid}
    revert_action = f"restore_target:{tid}"
    target_sql = (
        f", tgtupd AS (UPDATE ops.task_queue SET status='skipped', updated_at=now()"
        f"   WHERE id = {sql_str(tid)} RETURNING 1) SELECT 1;" if prior else " SELECT 1;"
    )
    _audit_and_finalize(cfg, row, result_obj, revert_action, snapshot, target_sql)
    return "executed"


def execute_bulk(cfg: dict, row: dict) -> str:
    raw, result = row["raw"], row["result"]
    if result.get("archived_count") is not None:
        return "already-done"
    snapshot = raw.get("snapshot") or []
    ids = [s["id"] for s in snapshot if s.get("status") == "pending"]
    if not ids:
        _set_error(row["id"], "bulk_archive_stale: empty/again snapshot")
        return "error"
    id_list = ", ".join(sql_str(i) for i in ids)
    result_obj = {"archived_count": len(ids)}
    revert_action = "restore_bulk"
    target_sql = (
        f", tgtupd AS (UPDATE ops.task_queue SET status='skipped', updated_at=now()"
        f"   WHERE id IN ({id_list}) AND status='pending' RETURNING 1) SELECT 1;"
    )
    _audit_and_finalize(cfg, row, result_obj, revert_action, {"snapshot": snapshot},
                        target_sql)
    return "executed"


_DISPATCH = {
    "route_to_tw": execute_route,
    "archive_noise": execute_archive_noise,
    "bulk_archive_stale": execute_bulk,
}


def run_executor(cfg: dict, verbose: bool) -> int:
    rows = fetch_approved(cfg)
    if verbose:
        log(TAG, f"approved rows: {len(rows)}")
    done = 0
    for row in rows:
        action = row["action"]
        if action not in ALLOWED:
            _set_error(row["id"], f"unknown action '{action}' (not in allow-list)")
            log(TAG, f"REFUSE {row['id'][:8]} unknown action '{action}'")
            continue
        try:
            outcome = _DISPATCH[action](cfg, row)
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the batch
            _set_error(row["id"], f"executor exception: {e}")
            log(TAG, f"ERROR {row['id'][:8]} {action}: {e}")
            continue
        log(TAG, f"{outcome:>10} {row['id'][:8]} {action}")
        if outcome == "executed":
            done += 1
    log(TAG, f"done: executed={done}/{len(rows)}")
    return 0


# --------------------------------------------------------------------------- #
# Revert handler (spec 09 §4.5)
# --------------------------------------------------------------------------- #
def resolve_executed(cfg: dict, ident: str) -> list[dict]:
    prod = sql_str(cfg["producer_source"])
    ident = ident.strip().lower()
    match = (f"left(id::text,8) = {sql_str(ident)}" if len(ident) == 8
             else f"id::text = {sql_str(ident)}")
    sql = (
        "SELECT COALESCE(jsonb_agg(jsonb_build_object("
        "  'id', id::text, 'action', extracted_action, 'revert_action', revert_action,"
        "  'snapshot', snapshot_before, 'raw', raw_content,"
        "  'executed_at', executed_at::text, 'reverted_at', reverted_at::text"
        ") ORDER BY id), '[]')::text "
        f"FROM ops.task_queue WHERE source = {prod} AND {match} "
        "AND executed_at IS NOT NULL;"
    )
    return psql_json(sql) or []


def _do_revert_action(cfg: dict, revert_action: str, row: dict) -> list[str]:
    notes: list[str] = []
    for part in [p for p in revert_action.split(";") if p]:
        if part.startswith("tw_delete:"):
            uuid = part.split(":", 1)[1]
            ok = tw_delete(cfg, uuid)
            notes.append(f"tw_delete:{uuid[:8]}={'ok' if ok else 'FAILED'}")
        elif part.startswith("restore_target:"):
            tid = part.split(":", 1)[1]
            snap = row.get("snapshot") or {}
            prior = snap.get("target_prior_status") or "pending"
            psql(f"UPDATE ops.task_queue SET status={sql_str(prior)}, updated_at=now() "
                 f"WHERE id = {sql_str(tid)};")
            notes.append(f"restore_target:{tid[:8]}->{prior}")
        elif part == "restore_bulk":
            snap = (row.get("raw") or {}).get("snapshot") or []
            n = 0
            for s in snap:
                psql(f"UPDATE ops.task_queue SET status={sql_str(s['status'])}, "
                     f"updated_at=now() WHERE id = {sql_str(s['id'])} AND status='skipped';")
                n += 1
            notes.append(f"restore_bulk:{n}")
        else:
            notes.append(f"unknown_revert_part:{part}")
    return notes


def run_revert(cfg: dict, ident: str, by: str) -> int:
    rows = resolve_executed(cfg, ident)
    if not rows:
        log(TAG, f"no executed proposal matches '{ident}'")
        return 1
    if len(rows) > 1:
        log(TAG, f"ambiguous '{ident}' matches {len(rows)}")
        return 1
    row = rows[0]
    if row.get("reverted_at"):
        log(TAG, f"already reverted at {row['reverted_at']}")
        return 1

    # ops.revert() stamps reverted_at + writes the 'reverted' audit row and
    # RETURNS the revert_action to replay.
    action = psql_scalar(f"SELECT ops.revert({sql_str(row['id'])}, {sql_str(by)});")
    if not action:
        log(TAG, "ops.revert returned no action")
        return 1
    notes = _do_revert_action(cfg, action, row)
    log(TAG, f"reverted {row['id'][:8]} [{action}] -> {'; '.join(notes)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Mimir autonomy executor (task_triage)")
    ap.add_argument("--revert", metavar="ID", default=None,
                    help="revert an executed proposal (short id or uuid)")
    ap.add_argument("--by", default="owner")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = common.load_config(args.config)
    if args.revert:
        return run_revert(cfg, args.revert, args.by)
    return run_executor(cfg, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
