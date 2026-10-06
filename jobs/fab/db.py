#!/usr/bin/env python3
"""fab.db — Postgres access for the fabrication service (dependency-free).

Same pattern as the cracks-brief job: pipe SQL into the db-host-db container via
`docker exec` (no driver). Covers job CRUD (fab.jobs), the model cache
(fab.model_cache), the trust gate (ops.trust_tier), and the action audit
(ops.action_audit). All value interpolation goes through dollar-quoting or the
numeric/enum helpers — never raw f-string concatenation of free text.
"""

import json
import subprocess
import uuid

_DELIM = "$fabq$"          # dollar-quote tag; json.dumps/text never emit it


def _dq(s: str) -> str:
    """Dollar-quote an arbitrary string literal safely for psql."""
    if s is None:
        return "NULL"
    if _DELIM in s:
        s = s.replace(_DELIM, "")
    return f"{_DELIM}{s}{_DELIM}"


def _jsonb(obj) -> str:
    if obj is None:
        return "NULL"
    return f"{_dq(json.dumps(obj, ensure_ascii=False))}::jsonb"


def psql(sql: str, timeout: int = 45) -> str:
    """Run SQL inside db-host-db, return stdout. Raises on error."""
    proc = subprocess.run(
        ["docker", "exec", "-i", "db-host-db", "bash", "-c",
         'PGPASSWORD="$POSTGRES_PASSWORD" psql -U db-host -d db-host -qtAX '
         '-v ON_ERROR_STOP=1 -f -'],
        input=sql, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def query_json(sql: str):
    """Run a SELECT that yields a single json/jsonb value; parse it."""
    out = psql(sql).strip()
    return json.loads(out) if out else None


# --------------------------------------------------------------------------- #
# Trust gate (ops.trust_tier) — the safety-critical read
# --------------------------------------------------------------------------- #
def trust_tier(domain: str) -> str:
    """Enforceable tier for a domain, clamped by its max_tier ceiling.
    For 'fabrication' this is ALWAYS 'propose' (ceiling), i.e. start_print
    can never be auto-executed."""
    out = psql(f"SELECT ops.trust_tier('{domain}');").strip()
    return out or "propose"


def trust_pct(domain: str) -> int:
    out = psql(
        f"SELECT COALESCE((SELECT trust_pct FROM ops.trust_current "
        f"WHERE domain='{domain}'), 0);"
    ).strip()
    try:
        return int(out)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Action audit (ops.action_audit) — append-only
# --------------------------------------------------------------------------- #
def audit_insert(agent: str, surface: str, domain: str, action: str,
                 params: dict, decision: str, trust_pct_at: int, tier_at: str,
                 approver: str | None = None, idempotency_key: str | None = None,
                 result: dict | None = None) -> int:
    """Append an ops.action_audit row; return its id."""
    cols = ("agent, surface, domain, action, params, decision, trust_pct_at, "
            "tier_at, approver, idempotency_key, result")
    vals = (f"{_dq(agent)}, {_dq(surface)}, {_dq(domain)}, {_dq(action)}, "
            f"{_jsonb(params)}, {_dq(decision)}, {int(trust_pct_at)}, {_dq(tier_at)}, "
            f"{_dq(approver)}, {_dq(idempotency_key)}, {_jsonb(result)}")
    out = psql(f"INSERT INTO ops.action_audit ({cols}) VALUES ({vals}) RETURNING id;").strip()
    return int(out)


# ops.action_audit is APPEND-ONLY — each lifecycle transition (proposed ->
# executed/denied/failed) is a new audit_insert(), never an UPDATE.


# --------------------------------------------------------------------------- #
# Jobs (fab.jobs) — the gated flow state machine, persisted
# --------------------------------------------------------------------------- #
_JOB_COLS = ("id, created_at, updated_at, surface, prompt, kind, status, "
             "cache_hit, cache_model_id, scad_code, stl_path, preview_path, "
             "gcode_path, dimensions_mm, params, slice_summary, printer, audit_id, error")


def job_create(prompt: str, surface: str) -> str:
    jid = str(uuid.uuid4())
    psql(
        "INSERT INTO fab.jobs (id, surface, prompt, status) "
        f"VALUES ('{jid}', {_dq(surface)}, {_dq(prompt)}, 'new');"
    )
    return jid


def job_update(job_id: str, **fields) -> None:
    """Update whitelisted job columns. jsonb fields auto-encoded."""
    json_fields = {"dimensions_mm", "params", "slice_summary"}
    text_fields = {"kind", "status", "scad_code", "stl_path", "preview_path",
                   "gcode_path", "printer", "error"}
    int_fields = {"cache_model_id", "audit_id"}
    bool_fields = {"cache_hit"}
    sets = []
    for k, v in fields.items():
        if k in json_fields:
            sets.append(f"{k} = {_jsonb(v)}")
        elif k in text_fields:
            sets.append(f"{k} = {_dq(v)}")
        elif k in int_fields:
            sets.append(f"{k} = {'NULL' if v is None else int(v)}")
        elif k in bool_fields:
            sets.append(f"{k} = {'true' if v else 'false'}")
        else:
            raise ValueError(f"job_update: unknown field {k}")
    if not sets:
        return
    psql(f"UPDATE fab.jobs SET {', '.join(sets)} WHERE id = '{_valid_uuid(job_id)}';")


def jobs_by_status(status: str) -> list:
    rows = query_json(
        "SELECT coalesce(jsonb_agg(to_jsonb(j)), '[]'::jsonb) FROM (SELECT " + _JOB_COLS +
        f" FROM fab.jobs WHERE status = {_dq(status)}) j;"
    )
    return rows or []


def job_get(job_id: str) -> dict | None:
    row = query_json(
        "SELECT to_jsonb(j) FROM (SELECT " + _JOB_COLS +
        f" FROM fab.jobs WHERE id = '{_valid_uuid(job_id)}') j;"
    )
    return row


def _valid_uuid(s: str) -> str:
    return str(uuid.UUID(str(s)))   # raises on non-uuid -> injection guard


# --------------------------------------------------------------------------- #
# Model cache (fab.model_cache)
# --------------------------------------------------------------------------- #
def cache_search(embedding: list[float] | None, kind: str, min_similarity: float,
                 prompt: str = "", limit: int = 3) -> list[dict]:
    """Retrieve proven models. Embedding path (pgvector) when available, else a
    trigram similarity fallback on nl_prompt."""
    if embedding:
        vec = "'[" + ",".join(f"{x:.6f}" for x in embedding) + "]'::vector(768)"
        rows = query_json(
            "SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]') FROM fab.search_models("
            f"{vec}, {_dq(kind)}, {int(limit)}, {float(min_similarity)}) s;"
        )
        return rows or []
    # fallback: trigram on prompt (embedding service down)
    rows = query_json(
        "SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]') FROM ("
        "SELECT id, nl_prompt, kind, source_format, scad_code, stl_path, params, "
        "dimensions_mm, printer, print_count, "
        f"similarity(nl_prompt, {_dq(prompt)}) AS similarity "
        "FROM fab.model_cache "
        f"WHERE worked IS TRUE AND kind = {_dq(kind)} "
        f"AND similarity(nl_prompt, {_dq(prompt)}) >= {float(min_similarity)} "
        f"ORDER BY 11 DESC LIMIT {int(limit)}) s;"
    )
    return rows or []


def cache_insert(nl_prompt: str, kind: str, source_format: str, scad_code: str | None,
                 stl_path: str | None, params: dict, dimensions_mm: dict | None,
                 printer: str | None, slicer_profile: str | None,
                 embedding: list[float] | None, worked=None) -> int:
    vec = ("NULL" if not embedding
           else "'[" + ",".join(f"{x:.6f}" for x in embedding) + "]'::vector(768)")
    worked_sql = "NULL" if worked is None else ("true" if worked else "false")
    out = psql(
        "INSERT INTO fab.model_cache (nl_prompt, kind, source_format, scad_code, "
        "stl_path, params, dimensions_mm, printer, slicer_profile, embedding, worked, "
        "last_used_at) VALUES ("
        f"{_dq(nl_prompt)}, {_dq(kind)}, {_dq(source_format)}, {_dq(scad_code)}, "
        f"{_dq(stl_path)}, {_jsonb(params)}, {_jsonb(dimensions_mm)}, {_dq(printer)}, "
        f"{_dq(slicer_profile)}, {vec}, {worked_sql}, now()) RETURNING id;"
    ).strip()
    return int(out)


def cache_mark_worked(model_id: int, worked: bool) -> None:
    psql(
        f"UPDATE fab.model_cache SET worked = {'true' if worked else 'false'}, "
        f"print_count = print_count + 1, last_used_at = now() WHERE id = {int(model_id)};"
    )
