#!/usr/bin/env python3
"""Mimir pre-flight hook — UserPromptSubmit (spec 08 §3 v1).

Deterministic string-match of the prompt against a LOCAL alias cache; on a hit, ONE
scope-filtered GET /v1/stamps through the memory API and one context line per entity.
The hook makes NO judgment — it surfaces freshness facts; the in-session model decides
whether to follow them. Prompt text never leaves the machine; only matched entity keys
reach the API (which audits every read).

Fail-open by construction: ANY failure prints nothing and exits 0. A hook that can
block a session is worse than no hook.

Cache: ~/.local/state/mimir-aliases.json, stale-while-revalidate — an expired cache
still serves this prompt while a detached child refreshes it (no network on the
prompt path except the stamp GET itself, 300ms cap).
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = os.environ.get("MIMIR_API_BASE", "http://localhost:8410")
SECRETS_FILE = Path.home() / ".config/claude-mcp-secrets.env"
CACHE = Path.home() / ".local/state/mimir-aliases.json"
CACHE_TTL_S = 6 * 3600
STAMP_TIMEOUT_S = 0.3
REFRESH_TIMEOUT_S = 6
MIN_PROMPT_LEN = 8
MAX_KEYS_PER_GET = 8
MAX_LINES = 6
MIN_ALIAS_LEN = 2


def ingress_and_key():
    ingress = os.environ.get("MIMIR_MCP_INGRESS", "claude_code")
    var = f"MIMIR_KEY_{ingress.upper()}"
    key = os.environ.get(var, "")
    if not key and SECRETS_FILE.exists():
        # same fallback the MCP server grew (spec 08 ask #3): desktop-launched
        # sessions never inherit the shell env.
        m = re.search(rf'^export {var}="?([^"\n]+)"?$',
                      SECRETS_FILE.read_text(), re.MULTILINE)
        if m:
            key = m.group(1)
    return ingress, key


def load_cache():
    try:
        c = json.loads(CACHE.read_text())
        age = (datetime.now(tz=timezone.utc)
               - datetime.fromisoformat(c["fetched_at"])).total_seconds()
        return c.get("aliases", []), age
    except (OSError, ValueError, KeyError):
        return None, None


def spawn_refresh():
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--refresh"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def refresh_cache():
    ingress, key = ingress_and_key()
    if not key:
        return
    req = urllib.request.Request(
        f"{API_BASE}/v1/aliases?ingress={urllib.parse.quote(ingress)}",
        headers={"X-Ingress-Key": key},
    )
    with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode())
    aliases = sorted(
        {(a["alias_norm"].strip().lower(), a["entity_key"])
         for a in data.get("aliases", [])
         if len(a.get("alias_norm", "").strip()) >= MIN_ALIAS_LEN},
        key=lambda x: -len(x[0]),          # longest (most specific) first
    )
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "ingress": ingress,
        "aliases": aliases,
    }))
    tmp.rename(CACHE)


def match_entities(prompt, aliases):
    p = prompt.lower()
    hits, seen = [], set()
    for alias, entity_key in aliases:
        if entity_key in seen:
            continue
        digits = alias.lstrip("+")
        if alias.startswith("+") and digits.isdigit() and len(digits) >= 9:
            pattern = re.escape(digits)     # phone: match the digit run, +-agnostic
        else:
            pattern = rf"\b{re.escape(alias)}\b"
        if re.search(pattern, p):
            seen.add(entity_key)
            hits.append(entity_key)
            if len(hits) >= MAX_KEYS_PER_GET:
                break
    return hits


def fetch_stamps(keys):
    ingress, key = ingress_and_key()
    if not key:
        return []
    ents = ",".join(urllib.parse.quote(k, safe=":") for k in keys)  # '+' -> %2B (spec §6 gotcha)
    req = urllib.request.Request(
        f"{API_BASE}/v1/stamps?entities={ents}&ingress={urllib.parse.quote(ingress)}",
        headers={"X-Ingress-Key": key},
    )
    with urllib.request.urlopen(req, timeout=STAMP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode()).get("stamps", [])


def main():
    if "--refresh" in sys.argv:
        refresh_cache()
        return
    raw = sys.stdin.read()
    prompt = json.loads(raw).get("prompt", "")
    if len(prompt) < MIN_PROMPT_LEN or prompt.lstrip().startswith("/"):
        return
    aliases, age = load_cache()
    if aliases is None:
        spawn_refresh()                     # first run: build in background, serve nothing
        return
    if age > CACHE_TTL_S:
        spawn_refresh()                     # stale-while-revalidate
    keys = match_entities(prompt, aliases)
    if not keys:
        return
    stamps = fetch_stamps(keys)
    if not stamps:
        return
    lines = ["Mimir recency stamps for entities named in this prompt — if your context "
             "is older than a stamp, fetch from that source before answering "
             "(m365_mail -> m365 MCP, imessage -> imessage MCP):"]
    for s in stamps[:MAX_LINES]:
        name = s.get("display_name") or s.get("entity")
        srcs = ",".join(s.get("sources") or [])
        ek = s.get("event_kind")
        # Fix B: the event_kind (unanswered / reply_needed / ...) is the most actionable part
        # of the stamp — spec 08 §3 step 3 put it on this line as "(<event_kind>)". Restored.
        suffix = f" ({ek})" if ek else ""
        lines.append(f"- {name} ({s.get('estate')}): last update {s.get('last_event_at')} via {srcs}{suffix}")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)                         # fail open, always silent
