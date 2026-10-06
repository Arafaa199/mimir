#!/usr/bin/env python3
"""Mimir cracks-brief — the daily "what's falling through the cracks" brief.

Spec: mimir/specs/01-cracks-brief.md.  Serves the owner's #1 goal: surface
obligations/intentions at risk of being dropped, once daily, precision over recall.

v0 (this file): PG-resident sources (insights.cracks_candidates) + TaskWarrior
(tw-api), DETERMINISTIC scoring, suppression, Telegram delivery via the Odin shim,
NOTIFY-ONLY (no action buttons). No LLM ranks anything — the judgment lives in
cracks-config.json weights. An optional cheap-model phrasing pass is presentation
only (never re-ranks/adds/drops) and is OFF by default.

Pipeline:
    gather (PG fn + tw-api)
      -> normalise (estate / confidence / ownership, all config-driven)
      -> score  (Urgency + Staleness + Importance + Actionability, 0..100)
      -> suppress (ops.cracks_dismissed: dismiss cooldown + snooze)
      -> dedup (same obligation across sources -> keep highest-confidence)
      -> threshold + cap (>= score_threshold, <= caps.total, <= caps.per_section)
      -> format by horizon (Act today / This week / Heads-up / Possibly)
      -> deliver (Telegram shim)
      -> log (ops.cracks_runs)

Dependency-free by design (model-portability): Python stdlib only. DB access via
`docker exec db-host-db psql` (no driver); HTTP via urllib. Runs on db-host.

Usage:
    cracks_brief.py [--dry-run] [--day YYYY-MM-DD] [--config PATH] [--verbose]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# A Candidate/Config is a plain heterogeneous dict (kept legible, not over-typed).
Candidate = dict
Config = dict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "cracks-config.json")

# Telegram shim (Odin) — overridable via env (systemd EnvironmentFile / wrapper).
SHIM_URL = os.environ.get("TELEGRAM_SHIM_URL", "http://localhost:3340/send")
TW_API_KEY = os.environ.get("TW_API_KEY", "")


def log(msg: str) -> None:
    print(f"[cracks-brief] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str) -> Config:
    with open(path, "r") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Postgres access (dependency-free: pipe SQL into the db-host-db container)
# --------------------------------------------------------------------------- #
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


def fetch_pg_candidates(day: str | None) -> list[Candidate]:
    """insights.cracks_candidates() -> list of raw candidate dicts."""
    arg = "NULL" if day is None else f"'{day}'::date"
    out = psql(f"SELECT insights.cracks_candidates({arg})::text;").strip()
    if not out:
        return []
    payload = json.loads(out)
    return payload.get("candidates", []) or []


def fetch_suppressions() -> dict[str, dict]:
    """ops.cracks_dismissed -> {item_key: {dismissed_at, snooze_until}}."""
    out = psql(
        "SELECT coalesce(jsonb_agg(jsonb_build_object("
        "'item_key', item_key, "
        "'dismissed_at', to_char(dismissed_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
        "'snooze_until', to_char(snooze_until AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
        ")), '[]') FROM ops.cracks_dismissed;"
    ).strip()
    rows = json.loads(out) if out else []
    return {r["item_key"]: r for r in rows}


def fetch_recent_surfaced(today: date, hold_days: int) -> dict[str, int]:
    """item_key -> max score it was surfaced+delivered at in [today-hold_days, today).

    Feeds the resurface hold: a chronic item that scored high yesterday should not
    nag again today. Excludes today's (not-yet-logged) run and undelivered runs.
    """
    lo = (today - timedelta(days=hold_days)).isoformat()
    hi = today.isoformat()
    out = psql(
        "SELECT coalesce(jsonb_object_agg(item_key, max_score), '{}')::text FROM ("
        "  SELECT s->>'item_key' AS item_key, MAX((s->>'score')::int) AS max_score "
        "  FROM ops.cracks_runs r, jsonb_array_elements(r.surfaced) s "
        f"  WHERE r.delivered AND r.run_day >= '{lo}'::date AND r.run_day < '{hi}'::date "
        "  GROUP BY 1) t;"
    ).strip()
    return json.loads(out) if out else {}


def log_run(day: date, candidate_count: int, surfaced: list[dict],
            delivered: bool, channel: str, notes: str) -> None:
    """Append a row to ops.cracks_runs. surfaced = list of delivered item dicts."""
    surfaced_json = json.dumps(surfaced, ensure_ascii=False)
    # Dollar-quoting sidesteps all escaping; json.dumps never emits `$mimir$`.
    if "$mimir$" in surfaced_json or "$mimir_n$" in notes:
        notes, surfaced_json = "notes-contained-delimiter", "[]"
    sql = (
        "INSERT INTO ops.cracks_runs "
        "(run_day, candidate_count, surfaced_count, surfaced, delivered, channel, notes) "
        f"VALUES ('{day}', {candidate_count}, {len(surfaced)}, "
        f"$mimir${surfaced_json}$mimir$::jsonb, {'true' if delivered else 'false'}, "
        f"'{channel}', $mimir_n${notes}$mimir_n$);"
    )
    psql(sql)


# --------------------------------------------------------------------------- #
# TaskWarrior (tw-api) source
# --------------------------------------------------------------------------- #
def http_get_json(url: str, headers: dict | None = None, timeout: int = 15,
                  retries: int = 3, backoff: float = 1.5) -> dict:
    """GET JSON with retries — tw-api shells out to the `task` CLI, which can
    transiently 500/timeout under sync lock contention."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(backoff)
    raise last  # type: ignore[misc]


def fetch_tw_candidates(cfg: Config, today: date) -> list[Candidate]:
    """tw-api pending tasks -> candidate dicts, filtered to crack candidacy."""
    tw = cfg["sources"]["tw"]
    if not tw.get("enabled"):
        return []
    if not TW_API_KEY:
        log("tw source enabled but TW_API_KEY not set — skipping TaskWarrior")
        return []
    url = tw["api_url"].rstrip("/") + "/tasks"
    try:
        data = http_get_json(url, headers={"X-TW-Key": TW_API_KEY})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"tw-api unreachable ({e}) — continuing with PG sources only")
        return []
    idle_threshold = tw.get("idle_days_threshold", 7)
    surface_max = tw.get("surface_max_days", 21)

    candidates: list[Candidate] = []
    for t in data.get("tasks", []):
        due = parse_dt(t.get("due"))
        scheduled = parse_dt(t.get("scheduled"))
        modified = parse_dt(t.get("modified"))
        due_days = (due.date() - today).days if due else None
        idle_days = (today - modified.date()).days if modified else None

        overdue = due_days is not None and due_days <= 0
        due_soon = due_days is not None and 0 < due_days <= surface_max
        stale_unscheduled = (
            idle_days is not None and idle_days > idle_threshold and scheduled is None
        )
        if not (overdue or due_soon or stale_unscheduled):
            continue

        # Ownership is encoded as an "@owner:" description prefix in this vault
        # (no prefix = the owner's own task; "@other:"/"@Name:" = someone else's).
        owner, what = parse_owner_prefix(t.get("description", ""))
        candidates.append({
            "source": "tw",
            "source_id": t.get("uuid", ""),
            "confidence": "high",
            "kind": "task",
            "origin": "tw-api",
            "what": what,
            "due_at": due.date().isoformat() if due else None,
            "last_activity_at": modified.isoformat() if modified else None,
            "scheduled_at": scheduled.date().isoformat() if scheduled else None,
            "importance_hint": "work_task",
            "project": t.get("project") or None,
            "domain": None,
            "assignee": owner,
            "tw_priority": t.get("priority") or "",
            "tags": t.get("tags", []),
        })
    return candidates


# --------------------------------------------------------------------------- #
# Normalisation — estate / ownership (config-driven)
# --------------------------------------------------------------------------- #
def resolve_estate(cand: Candidate, cfg: Config) -> str:
    """work|personal. project map -> domain -> content keywords -> default."""
    est = cfg["estate"]
    if cand["source"] == "finance_payment":
        return "personal"
    project = (cand.get("project") or "").strip().lower()
    if project:
        if project in [p.lower() for p in est["work_projects"]]:
            return "work"
        if project in [p.lower() for p in est["personal_projects"]]:
            return "personal"
    domain = (cand.get("domain") or "").strip().lower()
    if domain == "work":
        return "work"
    if domain in [d.lower() for d in est["personal_domains"]]:
        return "personal"
    if domain == "personal" or not domain:
        text = (cand.get("what") or "").lower()
        if any(kw in text for kw in est["work_content_keywords"]):
            return "work"
        if domain == "personal":
            return "personal"
    return est.get("default", "work")


# Owner label may be multi-word ("@all team:", "@other:", "@Hussein:").
_OWNER_PREFIX = re.compile(r"^@([^:]{1,30}):\s*")


def parse_owner_prefix(desc: str) -> tuple[str, str]:
    """('@other: do X') -> ('other', 'do X'). No prefix -> ('self', desc)."""
    m = _OWNER_PREFIX.match(desc or "")
    if m:
        return m.group(1).strip().lower(), (desc[m.end():] or "").strip()
    return "self", (desc or "").strip()


def is_owned(cand: Candidate, cfg: Config) -> bool:
    """Keep only obligations the OWNER owes — the crack definition (point 1).

    Ownership is encoded differently per source:
      * finance_payment       -> always the owner's bill
      * tw                    -> '@owner:' description prefix (parsed to assignee)
      * task_queue commitment -> plaud 'assignee' field (self/other/unknown)
      * task_queue task       -> explicitly tracked = the owner's own
    """
    src = cand["source"]
    if src == "finance_payment":
        return True
    owner = (cand.get("assignee") or "").strip().lower()
    if src == "tw":
        return owner in ("self", "")
    if cand.get("kind") == "commitment":
        keep = [a.lower() for a in cfg["commitments"]["assignee_keep"]]
        return owner in keep
    return True


def item_key(cand: Candidate) -> str:
    raw = f"{cand['source']}|{cand['source_id']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _dedup_key(what: str | None) -> str:
    """Normalise an obligation's text so the same item across sources collapses."""
    s = _OWNER_PREFIX.sub("", what or "")
    s = re.sub(r"\+\S+", "", s)              # drop +tags
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60]


def dedup_candidates(scored: list[Candidate]) -> list[Candidate]:
    """Collapse the same obligation appearing in >1 source (e.g. plaud task_queue
    + its synced tw task). Keep the best instance: high-confidence over medium,
    then higher score."""
    rank = {"high": 1, "medium": 0}
    best: dict[str, Candidate] = {}
    for c in scored:
        key = _dedup_key(c.get("what")) or c["item_key"]
        cur = best.get(key)
        cand_rank = (rank.get(c.get("confidence"), 0), c["score"])
        if cur is None or cand_rank > (rank.get(cur.get("confidence"), 0), cur["score"]):
            best[key] = c
    return list(best.values())


# --------------------------------------------------------------------------- #
# Date parsing
# --------------------------------------------------------------------------- #
def parse_dt(s: str | None) -> datetime | None:
    """Parse ISO-8601 ('...Z') or TaskWarrior basic ('YYYYMMDDTHHMMSSZ')."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Scoring (deterministic — the judgment lives in cfg weights)
# --------------------------------------------------------------------------- #
def score_urgency(cand: Candidate, cfg: Config, today: date) -> int:
    u = cfg["scoring"]["urgency"]
    due = cand.get("due_at")
    if not due:
        return u["no_due_date"]
    days_until = (date.fromisoformat(due) - today).days
    if days_until <= 3 and days_until >= -u["overdue_peak_max_days"]:
        return u["due_within_3d_or_overdue"]  # due within 3d OR overdue up to peak
    if days_until < -u["overdue_peak_max_days"]:
        overdue_days = -days_until
        zero = u["overdue_decay_zero_days"]
        peak = u["overdue_peak_max_days"]
        if overdue_days >= zero:
            return 0
        frac = (zero - overdue_days) / float(zero - peak)
        return int(round(u["due_within_3d_or_overdue"] * max(0.0, frac)))
    if 4 <= days_until <= 7:
        return u["due_4_7d"]
    if 8 <= days_until <= 14:
        return u["due_8_14d"]
    return u["far_future"]  # 15..∞ (and any residual future bucket)


def score_staleness(cand: Candidate, cfg: Config, today: date) -> int:
    s = cfg["scoring"]["staleness"]
    la = cand.get("last_activity_at")
    if not la:
        return 0  # payments / no-activity items are not "stale"
    dt = parse_dt(la)
    if not dt:
        return 0
    idle = (today - dt.date()).days
    if idle < s["fresh_days"]:
        return 0
    if idle < s["prime_low_days"]:
        # ramp 0 -> max across [fresh_days, prime_low_days)
        span = max(1, s["prime_low_days"] - s["fresh_days"])
        return int(round(s["max"] * (idle - s["fresh_days"]) / span))
    if idle <= s["prime_high_days"]:
        return s["max"]
    if idle <= s["abandoned_days"]:
        # ramp max -> abandoned_score across (prime_high_days, abandoned_days]
        span = max(1, s["abandoned_days"] - s["prime_high_days"])
        frac = (idle - s["prime_high_days"]) / span
        return int(round(s["max"] - (s["max"] - s["abandoned_score"]) * frac))
    return s["abandoned_score"]


def _text_matches(text: str | None, needles: list[str]) -> bool:
    t = (text or "").lower()
    return any(n.lower() in t for n in needles)


def score_importance(cand: Candidate, cfg: Config) -> int:
    imp = cfg["scoring"]["importance"]
    kw = cfg["keywords"]
    text = cand.get("what") or ""
    hint = cand.get("importance_hint") or "generic"
    estate = cand.get("estate", "work")

    # base by kind/hint
    if cand["source"] == "finance_payment":
        base = imp["payment"]
        if cand.get("urgency_flag"):
            base += imp["payment_urgent_flag_bonus"]
    elif hint == "payment":
        base = imp["payment"]
    elif hint == "deadline" or _text_matches(text, kw["urgent_importance_bump"]):
        base = imp["deadline_keyword"]
    elif _text_matches(text, kw["reply_owed"]):
        base = imp["reply_owed"]
    elif hint == "work_task" or estate == "work":
        base = imp["work_task"]
    else:
        base = imp["generic_personal"]

    # reply owed to a key person -> max
    if _text_matches(text, kw["reply_owed"]) and cfg["key_people"]["names"]:
        if _text_matches(text, cfg["key_people"]["names"]):
            base = max(base, imp["reply_owed_key_person"])

    # urgent keyword bump (failing payment, "asap", deadline words, ...)
    if _text_matches(text, kw["urgent_importance_bump"]):
        base += imp["urgent_keyword_bonus"]

    # tw priority
    if cand["source"] == "tw":
        base += imp["tw_priority_bonus"].get(cand.get("tw_priority", ""), 0)

    # high triage confidence (task_queue)
    tc = cand.get("triage_confidence")
    if tc is not None and float(tc) >= imp["high_triage_confidence_at"]:
        base += imp["high_triage_confidence_bonus"]

    # ambiguous ownership penalty for commitments not clearly 'self'
    if cand.get("kind") == "commitment":
        full = [a.lower() for a in cfg["commitments"]["assignee_full_weight"]]
        if (cand.get("assignee") or "").strip().lower() not in full:
            base = base * imp["ambiguous_owner_factor"]

    return int(round(max(0, min(base, 25))))  # importance clamped to 0..25


def score_actionability(cand: Candidate, cfg: Config) -> int:
    a = cfg["scoring"]["actionability"]
    if _text_matches(cand.get("what"), cfg["keywords"]["blocked_awaiting"]):
        return a["blocked_or_awaiting"]
    if cand.get("tags") and any(
        _text_matches(tag, cfg["keywords"]["blocked_awaiting"]) for tag in cand["tags"]
    ):
        return a["blocked_or_awaiting"]
    return a["clear_one_step"]


def score_candidate(cand: Candidate, cfg: Config, today: date) -> Candidate:
    urg = score_urgency(cand, cfg, today)
    stl = score_staleness(cand, cfg, today)
    imp = score_importance(cand, cfg)
    act = score_actionability(cand, cfg)
    total = urg + stl + imp + act
    return {**cand, "score": total,
            "components": {"urgency": urg, "staleness": stl,
                           "importance": imp, "actionability": act}}


# --------------------------------------------------------------------------- #
# Suppression
# --------------------------------------------------------------------------- #
def is_suppressed(key: str, suppressions: dict[str, dict], cfg: Config,
                  now: datetime) -> bool:
    row = suppressions.get(key)
    if not row:
        return False
    snooze = parse_dt(row.get("snooze_until"))
    if snooze:
        return now < snooze
    dismissed = parse_dt(row.get("dismissed_at"))
    if dismissed:
        cooldown = timedelta(days=cfg["suppression"]["dismiss_cooldown_days"])
        return now < dismissed + cooldown
    return False


def apply_resurface_hold(cands: list[Candidate], cfg: Config, today: date) -> list[Candidate]:
    """Hold an item surfaced in the last N days UNLESS its score jumped.

    Anti-fatigue: the same chronic item surfacing identically day after day is
    noise, not signal (e.g. a 21-day-overdue task scored at the ~100 ceiling). We
    hold it for resurface_hold_days once delivered, then let it resurface. An item
    whose score RISES by >= resurface_score_jump over the max it last surfaced at
    (a real escalation toward a deadline) breaks through immediately. Config-gated:
    resurface_hold_days=0 disables (backward-compatible)."""
    supp = cfg.get("suppression", {})
    hold_days = int(supp.get("resurface_hold_days", 0))
    if hold_days <= 0:
        return cands
    jump = int(supp.get("resurface_score_jump", 15))
    recent = fetch_recent_surfaced(today, hold_days)
    if not recent:
        return cands
    kept: list[Candidate] = []
    for c in cands:
        prev = recent.get(c["item_key"])
        if prev is None or c["score"] >= prev + jump:
            kept.append(c)
    return kept


# --------------------------------------------------------------------------- #
# Horizon + capping
# --------------------------------------------------------------------------- #
def assign_horizon(cand: Candidate, today: date) -> str:
    due = cand.get("due_at")
    if not due:
        return "heads_up"
    days = (date.fromisoformat(due) - today).days
    if days <= 1:
        return "act_today"
    if days <= 7:
        return "this_week"
    return "heads_up"


def select_and_cap(scored: list[Candidate], cfg: Config, today: date) -> dict[str, list[Candidate]]:
    """Threshold, then distribute into sections with per-section + total caps.

    High-confidence items fill horizon sections; medium (gated) items fill the
    'possibly' section only with whatever total-cap room remains.
    """
    threshold = cfg["score_threshold"]
    per = cfg["caps"]["per_section"]
    total_cap = cfg["caps"]["total"]
    # Optionally hold back N slots for the top transcript commitment(s) even when
    # high-confidence items would fill the cap (default 0 = strict spec: high wins).
    reserve = cfg["caps"].get("reserve_possibly", 0)
    # Estate quota: guarantee the top-N personal items a shot before work fills up.
    personal_reserved = cfg["caps"].get("personal_reserved", 0)

    qualifying = [c for c in scored if c["score"] >= threshold]
    qualifying.sort(key=lambda c: c["score"], reverse=True)

    # Anything not explicitly "high" is treated as gated (goes to 'possibly').
    high = [c for c in qualifying if c.get("confidence") == "high"]
    medium = [c for c in qualifying if c.get("confidence") != "high"]

    # Front-load the top personal high-confidence items so a wall of work tasks
    # can't crowd out every personal crack. Still score-ordered within personal
    # and within the remainder; a reserved slot goes unused only if no personal
    # candidate qualifies.
    if personal_reserved > 0:
        reserved = [c for c in high if c.get("estate") == "personal"][:personal_reserved]
        reserved_ids = {c["item_key"] for c in reserved}
        high = reserved + [c for c in high if c["item_key"] not in reserved_ids]

    sections: dict[str, list[Candidate]] = {
        "act_today": [], "this_week": [], "heads_up": [], "possibly": []}
    high_budget = max(0, total_cap - reserve)
    used = 0
    for c in high:
        if used >= high_budget:
            break
        h = assign_horizon(c, today)
        if len(sections[h]) >= per:
            continue
        sections[h].append(c)
        used += 1
    remaining = total_cap - used
    for c in medium:
        if remaining <= 0:
            break
        if len(sections["possibly"]) >= per:
            break
        sections["possibly"].append(c)
        remaining -= 1
    return sections


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def why_surfaced(cand: Candidate, today: date) -> str:
    due = cand.get("due_at")
    if due:
        days = (date.fromisoformat(due) - today).days
        if days < 0:
            return f"overdue {-days}d"
        if days == 0:
            return "due today"
        if days == 1:
            return "due tomorrow"
        return f"due in {days}d"
    la = cand.get("last_activity_at")
    if la:
        dt = parse_dt(la)
        if dt:
            return f"idle {(today - dt.date()).days}d"
    return "flagged"


def format_line(cand: Candidate, today: date) -> str:
    estate = cand.get("estate", "?")
    what = " ".join((cand.get("what") or "").split())
    if len(what) > 88:
        what = what[:85].rstrip() + "…"
    # payments show amount inline
    if cand["source"] == "finance_payment" and cand.get("amount"):
        amt = f"{cand.get('currency','')} {int(round(float(cand['amount'])))}".strip()
        why = f"{amt} · {why_surfaced(cand, today)}"
    else:
        why = why_surfaced(cand, today)
    return f"• [{estate}] {what} · {why} · {cand['score']}"


def format_brief(sections: dict[str, list[Candidate]], cfg: Config, today: date) -> str:
    titles = cfg["delivery"]["section_titles"]
    header_date = today.strftime("%a %d %b")
    if not any(sections.values()):
        return cfg["delivery"]["empty_message"].format(date=header_date)

    lines = [f"{cfg['delivery']['header_emoji']} Cracks — {header_date}"]
    for key in ("act_today", "this_week", "heads_up", "possibly"):
        items = sections[key]
        if not items:
            continue
        lines.append("")
        lines.append(titles[key])
        for c in items:
            lines.append(format_line(c, today))
    return "\n".join(lines)


def render_actionable(sections: dict[str, list[Candidate]], cfg: Config, today: date
                      ) -> tuple[str, dict | None]:
    """Numbered brief + a Telegram inline keyboard (one ✅/💤/🗑 row per item).

    Numbering ties each keyboard row to its text line; callback_data carries the
    stable item_key so cracks_actions.py can resolve + suppress it. Returns
    (text, keyboard) — keyboard is None when there is nothing to surface."""
    titles = cfg["delivery"]["section_titles"]
    header_date = today.strftime("%a %d %b")
    if not any(sections.values()):
        return cfg["delivery"]["empty_message"].format(date=header_date), None
    lines = [f"{cfg['delivery']['header_emoji']} Cracks — {header_date}"]
    rows: list[list[dict]] = []
    n = 0
    for key in ("act_today", "this_week", "heads_up", "possibly"):
        items = sections[key]
        if not items:
            continue
        lines.append("")
        lines.append(titles[key])
        for c in items:
            n += 1
            line = format_line(c, today)
            line = line[2:] if line.startswith("• ") else line  # number replaces the bullet
            lines.append(f"{n}. {line}")
            k = c["item_key"]
            rows.append([
                {"text": f"✅ {n}", "callback_data": f"c:d:{k}"},
                {"text": f"💤 {n}", "callback_data": f"c:s:{k}"},
                {"text": f"🗑 {n}", "callback_data": f"c:x:{k}"},
            ])
    return "\n".join(lines), {"inline_keyboard": rows}


# --------------------------------------------------------------------------- #
# Optional cheap-model phrasing pass (presentation only)
# --------------------------------------------------------------------------- #
def maybe_phrase(text: str, cfg: Config) -> str:
    ph = cfg.get("phrasing", {})
    if not ph.get("enabled"):
        return text
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        log("phrasing enabled but OPENROUTER_API_KEY missing — sending as-is")
        return text
    try:
        body = json.dumps({
            "model": ph["model"],
            "messages": [
                {"role": "system", "content":
                 "Rewrite this daily 'cracks' brief into slightly warmer natural "
                 "language. PRESENTATION ONLY: do NOT reorder, add, drop, or "
                 "re-rank any item; keep every [estate] tag, every score, and the "
                 "section groupings exactly. Plain text, no markdown."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3, "max_tokens": 700,
        }).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode())
        content = out["choices"][0]["message"]["content"].strip()
        return content or text
    except Exception as e:  # noqa: BLE001 — phrasing is best-effort, never fatal
        log(f"phrasing pass failed ({e}) — sending deterministic text")
        return text


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def deliver(text: str) -> bool:
    body = json.dumps({"message": text}).encode()
    req = urllib.request.Request(
        SHIM_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


def deliver_actionable(text: str, keyboard: dict, token: str, chat_id: str) -> bool:
    """Send directly via Telegram sendMessage WITH the inline keyboard (the shim's
    /send is text-only). Falls back to the shim caller when token/chat are absent."""
    body = json.dumps({
        "chat_id": chat_id, "text": text, "reply_markup": keyboard,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_sections(cfg: Config, today: date, day_arg: str | None
                   ) -> tuple[dict[str, list[Candidate]], int]:
    raw: list[Candidate] = []
    if cfg["sources"]["task_queue"]["enabled"] or cfg["sources"]["finance_payment"]["enabled"]:
        raw.extend(fetch_pg_candidates(day_arg))
    raw.extend(fetch_tw_candidates(cfg, today))

    # source toggles (PG fn returns both task_queue + finance_payment)
    enabled = {s for s in ("task_queue", "finance_payment", "tw")
               if cfg["sources"][s]["enabled"]}
    raw = [c for c in raw if c["source"] in enabled]

    # normalise + ownership gate (keep only what the OWNER owes)
    normalised: list[Candidate] = []
    for c in raw:
        if not is_owned(c, cfg):
            continue
        c = {**c, "estate": resolve_estate(c, cfg), "item_key": item_key(c)}
        normalised.append(c)

    # suppression
    now = datetime.now(timezone.utc)
    suppressions = fetch_suppressions()
    live = [c for c in normalised if not is_suppressed(c["item_key"], suppressions, cfg, now)]

    # score -> dedup across sources -> resurface-hold (anti-repetition) -> cap
    scored = [score_candidate(c, cfg, today) for c in live]
    deduped = dedup_candidates(scored)
    fresh = apply_resurface_hold(deduped, cfg, today)
    sections = select_and_cap(fresh, cfg, today)
    return sections, len(normalised)


def flatten(sections: dict[str, list[Candidate]]) -> list[dict]:
    out: list[dict] = []
    for key in ("act_today", "this_week", "heads_up", "possibly"):
        for c in sections[key]:
            out.append({
                "section": key, "item_key": c["item_key"], "source": c["source"],
                "source_id": c["source_id"], "estate": c["estate"],
                "confidence": c["confidence"], "what": c["what"],
                "score": c["score"], "components": c["components"],
                "due_at": c.get("due_at"),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Mimir cracks-brief")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD override for 'today'")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.day:
        today = date.fromisoformat(args.day)
    else:
        today = date.fromisoformat(psql("SELECT life.dubai_today();").strip())

    sections, candidate_count = build_sections(cfg, today, args.day)
    surfaced = flatten(sections)

    # Actionable delivery (one-tap ✅/💤/🗑) when configured + the bot creds are
    # present; else the classic notify-only text via the shim.
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    actionable = bool(cfg["delivery"].get("actionable") and tg_token and tg_chat)
    if actionable:
        text, keyboard = render_actionable(sections, cfg, today)
    else:
        text, keyboard = format_brief(sections, cfg, today), None

    if args.verbose or args.dry_run:
        log(f"day={today} candidates={candidate_count} surfaced={len(surfaced)}")
        for item in surfaced:
            log(f"  [{item['section']}] {item['score']:>3} {item['components']} "
                f"{item['estate']}/{item['confidence']} :: {item['what'][:70]}")
        print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60 + "\n")

    if args.dry_run:
        log("dry-run: not delivering, not logging a run")
        return 0

    delivered = False
    notes = ""
    try:
        if actionable and keyboard is not None:
            delivered = deliver_actionable(text, keyboard, tg_token, tg_chat)
        else:
            delivered = deliver(maybe_phrase(text, cfg))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        notes = f"delivery failed: {e}"
        log(notes)

    try:
        log_run(today, candidate_count, surfaced, delivered,
                cfg["delivery"]["channel"], notes or f"surfaced={len(surfaced)}")
    except Exception as e:  # noqa: BLE001 — logging failure must not mask delivery
        log(f"failed to write ops.cracks_runs: {e}")

    log(f"done: surfaced={len(surfaced)} delivered={delivered}")
    return 0 if delivered or not surfaced else 1


if __name__ == "__main__":
    sys.exit(main())
