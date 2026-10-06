#!/usr/bin/env python3
"""Mimir telegram-dispatcher — the SINGLE getUpdates consumer for the owner's Odin bot.

Telegram allows exactly one getUpdates poller per bot, so this one service owns the
poll and fans every update out to the right handler:

  1. callback_query (cracks-brief button taps) -> handle_callback():
       ✅ done    -> suppress the item + close its TaskWarrior task (if a tw task)
       💤 snooze  -> hide it for CRACKS_SNOOZE_DAYS
       🗑 dismiss -> hide it for the dismiss cooldown
     State lands in ops.cracks_dismissed (the brief's is_suppressed() consults it),
     so a handled crack stops reappearing. callback_data = "c:<d|s|x>:<item_key>";
     item_key is a one-way hash resolved from the latest ops.cracks_runs.surfaced row.

  2. text message (owner DM) -> handle_message(): TWO-WAY ODIN over Telegram.
     Routes the text to the Odin "Demon" assistant (the SAME Claude-backed
     assistant used over WhatsApp — worker:3345 /chat), persists the returned
     session_id for conversation continuity, and relays the reply back. This is why
     Odin finally answers on Telegram: before, Odin had NO telegram inbound path and
     this poller silently ate every text. The /chat call runs in a background thread
     (guarded by _chat_lock) so a long task — "go through my gmail" — does not block
     button taps or new messages. Commands: /new (reset session), /help.

Security: only the configured owner (TELEGRAM_CHAT_ID) is acted on, for both paths.
Dependency-free (stdlib only). DB via `docker exec db-host-db psql`. Runs on db-host.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
TW_API = os.environ.get("TW_API_URL", "http://localhost:8250").rstrip("/")
TW_KEY = os.environ.get("TW_API_KEY", "")
SNOOZE_DAYS = int(os.environ.get("CRACKS_SNOOZE_DAYS", "7"))
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = os.path.expanduser("~/.local/state/cracks-actions.offset")

# Two-way Odin: route owner text to the Demon assistant (same one WhatsApp uses).
CHAT_URL = os.environ.get("ODIN_CHAT_URL", "http://localhost:3345/chat").rstrip("/")
CHAT_MODEL = os.environ.get("ODIN_CHAT_MODEL", "sonnet")
CHAT_HTTP_TIMEOUT = int(os.environ.get("ODIN_CHAT_TIMEOUT", "650"))  # > bridge-side kill
SESSION_STATE = os.path.expanduser("~/.local/state/cracks-actions.session")
TG_MAX = 3900  # Telegram hard limit is 4096; leave headroom
_chat_lock = threading.Lock()  # one Odin chat at a time (avoids --resume race on one session)

VERB = {"d": ("done", "✅ Done"), "s": ("snooze", "💤 Snoozed"), "x": ("dismiss", "🗑 Dismissed")}

HELP_TEXT = (
    "🤖 *Odin on Telegram*\n"
    "Just send a message and I'll act on it (same assistant as WhatsApp).\n\n"
    "/new — start a fresh conversation (clears context)\n"
    "/help — this message\n\n"
    "Cracks-brief buttons (✅ 💤 🗑) still work as before."
)


def log(m: str) -> None:
    print(f"[cracks-actions] {m}", file=sys.stderr, flush=True)


def psql(sql: str, timeout: int = 30) -> str:
    p = subprocess.run(
        ["docker", "exec", "-i", "db-host-db", "bash", "-c",
         'PGPASSWORD="$POSTGRES_PASSWORD" psql -U db-host -d db-host -qtAX -v ON_ERROR_STOP=1 -f -'],
        input=sql, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"psql rc={p.returncode}: {p.stderr.strip()}")
    return p.stdout


def sql_lit(s) -> str:
    """Dollar-quoted SQL literal; NULL for None. Sidesteps all quote escaping."""
    if s is None:
        return "NULL"
    s = str(s)
    if "$q$" in s:
        return "'" + s.replace("'", "''") + "'"
    return "$q$" + s + "$q$"


def tg(method: str, params: dict, timeout: int = 60) -> dict:
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def resolve_item(item_key: str) -> dict | None:
    """Latest surfaced entry for item_key -> {source, source_id, what} (item_key is a hash)."""
    out = psql(
        "SELECT coalesce((SELECT jsonb_build_object("
        "'source', s->>'source', 'source_id', s->>'source_id', 'what', s->>'what') "
        "FROM ops.cracks_runs r, jsonb_array_elements(r.surfaced) s "
        f"WHERE s->>'item_key' = {sql_lit(item_key)} "
        "ORDER BY r.run_day DESC LIMIT 1), 'null')::text;").strip()
    return json.loads(out) if out else None


def suppress(item_key: str, item: dict | None, reason: str, snooze_until: datetime | None) -> None:
    src = sql_lit((item or {}).get("source"))
    sid = sql_lit((item or {}).get("source_id"))
    what = sql_lit((item or {}).get("what"))
    snz = f"'{snooze_until.isoformat()}'::timestamptz" if snooze_until else "NULL"
    psql(
        "INSERT INTO ops.cracks_dismissed "
        "(item_key, source, source_id, what, dismissed_at, snooze_until, reason) "
        f"VALUES ({sql_lit(item_key)}, {src}, {sid}, {what}, now(), {snz}, {sql_lit(reason)}) "
        "ON CONFLICT (item_key) DO UPDATE SET dismissed_at = EXCLUDED.dismissed_at, "
        "snooze_until = EXCLUDED.snooze_until, reason = EXCLUDED.reason, "
        "source = COALESCE(ops.cracks_dismissed.source, EXCLUDED.source), "
        "source_id = COALESCE(ops.cracks_dismissed.source_id, EXCLUDED.source_id), "
        "what = COALESCE(EXCLUDED.what, ops.cracks_dismissed.what);")


def tw_done(uuid: str) -> bool:
    req = urllib.request.Request(f"{TW_API}/tasks/{uuid}/done", data=b"{}",
                                 headers={"X-TW-Key": TW_KEY, "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log(f"tw done {uuid[:8]} http {e.code}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"tw done {uuid[:8]} err {e}")
        return False


def handle_callback(cb: dict) -> None:
    cbid = cb.get("id")
    frm = str(cb.get("from", {}).get("id", ""))
    cid = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    data = cb.get("data", "")
    if OWNER and OWNER not in (frm, cid):
        tg("answerCallbackQuery", {"callback_query_id": cbid, "text": "not authorized"})
        return
    if not data.startswith("c:"):
        tg("answerCallbackQuery", {"callback_query_id": cbid})
        return
    try:
        _, verb, item_key = data.split(":", 2)
    except ValueError:
        tg("answerCallbackQuery", {"callback_query_id": cbid, "text": "bad action"})
        return
    if verb not in VERB:
        tg("answerCallbackQuery", {"callback_query_id": cbid, "text": "unknown action"})
        return
    name, toast = VERB[verb]
    item = resolve_item(item_key)
    snooze_until = datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS) if verb == "s" else None
    try:
        suppress(item_key, item, name, snooze_until)
        if verb == "d" and item and item.get("source") == "tw" and item.get("source_id"):
            tw_done(item["source_id"])
    except Exception as e:  # noqa: BLE001 — one bad action must not kill the poller
        log(f"action {name} {item_key} failed: {e}")
        tg("answerCallbackQuery", {"callback_query_id": cbid, "text": "⚠️ failed — try again"})
        return
    what = (item or {}).get("what") or ""
    label = f"💤 Snoozed {SNOOZE_DAYS}d" if verb == "s" else toast
    tg("answerCallbackQuery", {"callback_query_id": cbid,
                               "text": label + (f": {what[:38]}" if what else "")})
    log(f"{name} item={item_key} tw={'yes' if (item or {}).get('source')=='tw' and verb=='d' else 'no'}")


def load_session() -> str | None:
    try:
        with open(SESSION_STATE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_session(sid: str) -> None:
    os.makedirs(os.path.dirname(SESSION_STATE), exist_ok=True)
    with open(SESSION_STATE, "w") as f:
        f.write(sid)


def clear_session() -> None:
    try:
        os.remove(SESSION_STATE)
    except OSError:
        pass


def send_chunked(chat_id: str, text: str) -> None:
    text = text or "(no response)"
    for i in range(0, len(text), TG_MAX):
        try:
            tg("sendMessage", {"chat_id": chat_id, "text": text[i:i + TG_MAX]})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"sendMessage failed: {e}")
            return


def ask_odin(message: str, session_id: str | None) -> tuple[str, str | None]:
    """POST to the Demon assistant; returns (reply_text, session_id)."""
    payload = {"message": message, "model": CHAT_MODEL}
    if session_id:
        payload["session_id"] = session_id
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CHAT_HTTP_TIMEOUT) as r:
            resp = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"⚠️ Odin unreachable: {e}", session_id
    if resp.get("success"):
        return resp.get("response") or "(no response)", resp.get("session_id", session_id)
    return f"⚠️ Odin error: {resp.get('error', 'unknown')}", resp.get("session_id", session_id)


def _run_chat(chat_id: str, text: str) -> None:
    """Worker thread: ask Odin, relay reply. Owns _chat_lock (released here)."""
    try:
        try:
            tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001 — typing indicator is best-effort
            pass
        reply, new_sid = ask_odin(text, load_session())
        if new_sid:
            save_session(new_sid)
        send_chunked(chat_id, reply)
    except Exception as e:  # noqa: BLE001 — a bad chat must not kill the poller
        log(f"chat error: {e}")
        try:
            send_chunked(chat_id, f"⚠️ error: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _chat_lock.release()


def handle_message(msg: dict) -> None:
    frm = str(msg.get("from", {}).get("id", ""))
    chat = msg.get("chat", {})
    cid = str(chat.get("id", ""))
    if OWNER and OWNER not in (frm, cid):
        return  # not the owner — ignore silently
    text = (msg.get("text") or "").strip()
    if not text:
        return  # non-text (photo/sticker/etc.) — ignore
    low = text.lower()
    if low in ("/new", "/reset"):
        clear_session()
        send_chunked(cid, "🆕 New conversation started.")
        return
    if low in ("/help", "/start"):
        try:
            tg("sendMessage", {"chat_id": cid, "text": HELP_TEXT, "parse_mode": "Markdown"})
        except (urllib.error.URLError, TimeoutError, OSError):
            send_chunked(cid, HELP_TEXT)
        return
    if not _chat_lock.acquire(blocking=False):
        send_chunked(cid, "⏳ Still working on your previous message — one sec…")
        return
    threading.Thread(target=_run_chat, args=(cid, text), daemon=True).start()


def load_offset() -> int | None:
    try:
        with open(STATE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def save_offset(o: int) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        f.write(str(o))


def main() -> int:
    if not TOKEN:
        log("TELEGRAM_BOT_TOKEN missing — exiting")
        return 1
    log(f"up (owner={OWNER or 'ANY'}, snooze={SNOOZE_DAYS}d, tw={TW_API}, odin={CHAT_URL})")
    offset = load_offset()
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            resp = tg("getUpdates", params, timeout=60)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"getUpdates err {e}")
            time.sleep(5)
            continue
        if not resp.get("ok"):
            log(f"getUpdates not ok: {resp.get('description')}")
            time.sleep(5)
            continue
        updates = resp.get("result", [])
        for upd in updates:
            offset = upd["update_id"] + 1  # advance past EVERY update (incl. non-callbacks)
            cb = upd.get("callback_query")
            msg = upd.get("message")
            try:
                if cb:
                    handle_callback(cb)
                elif msg:
                    handle_message(msg)
            except Exception as e:  # noqa: BLE001 — one bad update must not kill the poller
                log(f"handle err {e}")
        if updates:
            save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())
