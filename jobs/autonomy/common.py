#!/usr/bin/env python3
"""Shared helpers for the Mimir autonomy loop (spec 09, task_triage at propose).

Dependency-free by design (model-portability): Python stdlib only. DB access via
`docker exec db-host-db psql` when run ON db-host, or transparently over `ssh db-host`
when run elsewhere (so verdict.py works from laptop). HTTP via urllib.

Nothing in this package can auto-execute anything: the mig-306 gate refuses every
row while the kill switch is OFF and task_triage sits at propose (0% trust). These
helpers just make the propose -> approve -> execute -> revert loop legible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "autonomy-config.json")

# Odin Telegram shim + tw-api (overridable via env / config; same rails cracks uses)
SHIM_URL = os.environ.get("TELEGRAM_SHIM_URL", "http://localhost:3340/send")
TW_API_KEY = os.environ.get("TW_API_KEY", "")


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, "r") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Postgres access — local docker-exec on db-host, ssh fallback anywhere else.
# SQL always travels via stdin (-f -), never -c, so quoting can't mangle it
# (the migrate.sh lesson, 2026-07-11).
# --------------------------------------------------------------------------- #
_PSQL_INNER = ('PGPASSWORD="$POSTGRES_PASSWORD" psql -U db-host -d db-host '
               "-qtAX -v ON_ERROR_STOP=1 -f -")
_LOCAL: bool | None = None


def _db_host_db_local() -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "db-host-db"],
            capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def _is_local() -> bool:
    global _LOCAL
    if _LOCAL is None:
        _LOCAL = _db_host_db_local()
    return _LOCAL


def psql(sql: str, timeout: int = 90) -> str:
    """Run SQL inside db-host-db, return stdout. Raises RuntimeError on failure."""
    if _is_local():
        cmd = ["docker", "exec", "-i", "db-host-db", "bash", "-c", _PSQL_INNER]
    else:
        cmd = ["ssh", "db-host", f"docker exec -i db-host-db bash -c '{_PSQL_INNER}'"]
    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def psql_scalar(sql: str, timeout: int = 90) -> str:
    return psql(sql, timeout).strip()


def psql_json(sql: str, timeout: int = 90):
    """Run a query whose single output cell is JSON; parse and return it."""
    out = psql_scalar(sql, timeout)
    if not out:
        return None
    return json.loads(out)


# --------------------------------------------------------------------------- #
# SQL literal helpers
# --------------------------------------------------------------------------- #
def sql_str(s: str) -> str:
    """Single-quote a string as a SQL literal (doubling embedded quotes)."""
    return "'" + str(s).replace("'", "''") + "'"


_DOLLAR_TAG = "$aut$"


def sql_jsonb(obj) -> str:
    """Embed a Python object as a jsonb literal via dollar-quoting.

    json.dumps never emits the `$aut$` delimiter unless the source text literally
    contains it; guard for that vanishingly-rare case (cracks-brief pattern).
    """
    s = json.dumps(obj, ensure_ascii=False)
    if _DOLLAR_TAG in s:
        # fall back to a safe, escaped literal
        return sql_str(s) + "::jsonb"
    return f"{_DOLLAR_TAG}{s}{_DOLLAR_TAG}::jsonb"


# --------------------------------------------------------------------------- #
# HTTP (tw-api + Telegram shim)
# --------------------------------------------------------------------------- #
def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None, timeout: int = 20,
              retries: int = 1, backoff: float = 2.0) -> tuple[int, dict]:
    """One JSON request. Returns (status, parsed_body). Retries on transient
    network / 5xx (tw-api shells to the `task` CLI, which 500s under sync-lock)."""
    data = json.dumps(body).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()[:300]
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(backoff)
                continue
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"error": raw}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(backoff)
    raise last  # type: ignore[misc]


def deliver_telegram(text: str, timeout: int = 15) -> bool:
    """Send one message via the Odin shim (same rail as cracks-brief)."""
    status, _ = http_json("POST", SHIM_URL, {"message": text}, timeout=timeout)
    return 200 <= status < 300
