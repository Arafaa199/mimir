#!/usr/bin/env python3
"""mimir-memory MCP server — the brain, as tools, inside Claude Code (spec 07).

    claude mcp add mimir -- python3 ~/path/mcp_server.py

## The one design rule, and why it shapes this file

**The model must never choose its own scope.** §4 is explicit: scope is minted from the
authenticated transport, below the model, and an LLM that can talk itself into another
estate is an LLM an injected message can talk into another estate.

An MCP tool parameter is, by construction, *chosen by the model*. So this server exposes
NO parameter that can widen access:

  * there is no `escalate` argument — cross-estate is enabled by an ENV VAR you set when
    you LAUNCH the session (`MIMIR_MCP_CROSS_ESTATE=true`). That is a deliberate human act,
    outside the model's reach, and it is exactly "scope is fixed at session creation";
  * there is no `estate`/`datasets` argument — the session's estate comes from
    `MIMIR_MCP_INGRESS` (default `claude_code` = personal + shared; set `work_cli` in a work
    repo). Again: chosen by you, at launch;
  * there is no `declassify` argument on remember — writing to `shared` makes a fact visible
    from BOTH estates, so it stays a deliberate act you perform yourself, not something a
    model can decide mid-thought.

What the model CAN do is ask questions and write down facts, within the walls you set before
it started. That is the whole point.

Zero dependencies (stdlib only) so it cannot break on a machine where a venv drifted.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("MIMIR_API", "http://localhost:8410")
INGRESS = os.environ.get("MIMIR_MCP_INGRESS", "claude_code")
CROSS_ESTATE = os.environ.get("MIMIR_MCP_CROSS_ESTATE", "").lower() == "true"
TIMEOUT = int(os.environ.get("MIMIR_MCP_TIMEOUT", "240"))


def _key_for(ingress: str) -> str:
    """The ingress key, from process env OR the central secrets file (spec 08 §6.3).

    A DESKTOP-launched Claude Code (or any session started outside the login shell) never
    inherits `MIMIR_KEY_*` — the recall MCP then silently dies with no key while the API is
    perfectly healthy, and because the read path has no habitual readers the break is
    invisible. Found live in the fable-setup session: key was in `claude-mcp-secrets.env` all
    along. So fall back to sourcing that file rather than depending on env inheritance.
    Read-only, parsed here (no shell): the file is `export VAR=value` lines, chmod 600.
    """
    names = (f"MIMIR_KEY_{ingress.upper()}", "MIMIR_KEY_CLAUDE_CODE")
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    secrets = os.path.expanduser(
        os.environ.get("MIMIR_SECRETS_FILE", "~/.config/claude-mcp-secrets.env"))
    try:
        with open(secrets) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                k, _, val = line.partition("=")
                if k.strip() in names and val:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


KEY = _key_for(INGRESS)

TOOLS = [
    {
        "name": "memory_recall",
        "description": (
            "Search the owner's one memory (the Mimir spine: a knowledge graph + vector store "
            "over his vault, agent memories, transcripts and work). Use this whenever a "
            "question depends on something HE knows, decided, or was told — his homelab, "
            "his projects, his infrastructure, past decisions and their reasons — rather "
            "than on general knowledge. Returns retrieved facts and passages; YOU do the "
            "reasoning over them. Runs entirely on his hardware and calls no LLM, so it is "
            "free and private. Note: it can only answer from the estates THIS session is "
            "scoped to (call memory_scope to see which)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "A natural-language question or topic."},
                "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20,
                      "description": "How many passages per estate."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "Write a durable fact into the owner's one memory, so every future session on every "
            "surface (terminal, phone, glasses) knows it. Use it for decisions and their "
            "REASONS, gotchas discovered the hard way, and facts that were expensive to "
            "learn — not for transient chatter or anything you can re-derive from the code. "
            "Be precise: what you write becomes a belief this brain will repeat. It is "
            "recallable immediately; the graph enriches it later. Material that classifies "
            "as client/NDA'd is automatically kept pointer-only and never sent to any LLM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The fact. Self-contained — it will be read years "
                                        "from now with no conversation around it."},
                "title": {"type": "string", "description": "Short label."},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_scope",
        "description": (
            "Show which estates THIS session can see and write, and why. Use it when you are "
            "unsure whether a missing answer means 'the brain does not know' or 'this session "
            "is not allowed to see that estate' — those are very different, and you should "
            "never guess between them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _post(path: str, body: dict) -> dict:
    if not KEY:
        raise RuntimeError(
            f"no key for ingress {INGRESS!r}. Set MIMIR_KEY_{INGRESS.upper()} "
            f"(it lives in ~/.config/claude-mcp-secrets.env)."
        )
    req = urllib.request.Request(
        f"{API}/v1/{path}",
        data=json.dumps({"ingress": INGRESS, **body}).encode(),
        headers={"Content-Type": "application/json", "X-Ingress-Key": KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"mimir-memory said {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"cannot reach mimir-memory at {API} ({e.reason}). Is the tailnet up, and is "
            f"mimir-memory running on db-host?"
        ) from None


def _fmt_recall(r: dict) -> str:
    scope = r["scope"]
    lines = [
        f"scope: {', '.join(scope['datasets'])}"
        + (" (CROSS-ESTATE)" if scope["cross_estate"] else "")
        + f" | {r['count']} passage(s)"
    ]
    # Surface degradation HONESTLY. A brain that quietly answers from half its memory is
    # worse than one that says which half it used.
    for store, state in r.get("health", {}).items():
        if state != "ok":
            lines.append(f"!! {store} unavailable ({state[:60]}) — answered without it")
    if not r["passages"]:
        lines.append("(nothing found in the estates this session can see)")
    fresh = r.get("freshness") or []
    if fresh:
        lines.append("")
        for f in fresh:
            src = ", ".join(f.get("sources") or []) or "?"
            lines.append(f"\u23f1 {f.get('display_name') or f['entity']} — last real-world activity "
                         f"{f.get('last_event_at') or '?'} via {src} (check the live source before "
                         f"answering time-sensitively)")
    for i, p in enumerate(r["passages"], 1):
        tag = "POINTER — content withheld" if p["pointer_only"] else p["origin"]
        lines.append(f"\n[{i}] ({p['estate']}/{tag})\n{p['text']}")
    return "\n".join(lines)


def _call(name: str, args: dict) -> str:
    if name == "memory_recall":
        body = {"query": args["query"], "k": args.get("k", 5)}
        if CROSS_ESTATE:
            # Set by the HUMAN at launch, never by the model. This is the whole point.
            body["escalate"] = True
        return _fmt_recall(_post("recall", body))

    if name == "memory_remember":
        r = _post("remember", {
            "text": args["text"],
            "title": args.get("title", ""),
            "tags": args.get("tags") or [],
            "source": "claude-code",
        })
        state = "recallable now" if r["recallable"] else "stored"
        graph = "in the graph" if r["cognified"] else "graph pending (drain will enrich)"
        return (f"remembered -> {r['dataset']} ({r['sensitivity']}), {state}, {graph}\n"
                f"{r['note']}\nsha256={r['sha256'][:16]}")

    if name == "memory_scope":
        r = _post("recall", {"query": "scope probe", "k": 1,
                             **({"escalate": True} if CROSS_ESTATE else {})})
        s = r["scope"]
        return (
            f"ingress: {s['ingress']}\n"
            f"can see: {', '.join(s['datasets'])}\n"
            f"cross-estate: {s['cross_estate']} (set by MIMIR_MCP_CROSS_ESTATE at launch, "
            f"never by the model)\n"
            f"answers may go to: {s['sink']}\n"
            f"stores: {r.get('health')}"
        )

    raise RuntimeError(f"unknown tool {name!r}")


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, mid = req.get("method"), req.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mimir-memory", "version": "0.1"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params", {})
            try:
                text = _call(params.get("name", ""), params.get("arguments") or {})
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as exc:  # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                    "isError": True,
                }})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": f"unknown method {method}"}})
        # notifications (no id) need no reply


if __name__ == "__main__":
    main()
