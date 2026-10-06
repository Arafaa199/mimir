# Mimir pre-flight hook (spec 08 §3 v1)

`UserPromptSubmit` hook for Claude Code on laptop: string-matches the prompt against a
local alias cache; on a hit, ONE scope-filtered `GET /v1/stamps` and one context line per
entity. **No judgment, no LLM, fail-open always** — any failure prints nothing, exits 0,
never blocks a prompt. Prompt text never leaves the machine; only matched entity keys
reach the API (audited there).

## Wiring

| Piece | Where |
|---|---|
| Script | `preflight_hook.py` (stdlib-only; `--refresh` mode = cache builder) |
| Registration | `~/.claude/settings.json` → `hooks.UserPromptSubmit` (timeout 5s) |
| Alias cache | `~/.local/state/mimir-aliases.json` — TTL 6h, **stale-while-revalidate**: an expired cache still serves the current prompt while a detached child refreshes |
| Ingress/key | `MIMIR_MCP_INGRESS` (default `claude_code`) → `MIMIR_KEY_<INGRESS>`; falls back to parsing `claude-mcp-secrets.env` (desktop-launch env gap, ask #3 pattern) |

## Behavior

- Skips prompts <8 chars and `/slash` commands. No network on the no-match path.
- Word-boundary matching (phones match by digit-run); longest-alias-first; ≤8 keys per
  GET (300ms cap), ≤6 injected lines. `+` in entity keys URL-encoded (`%2B` — spec §6 gotcha).
- **Scope holds at the cache layer**: `/v1/aliases` is server-side scope-filtered, so work
  entities never even enter a personal session's cache (existence is signal).
- Work sessions get work stamps once `MIMIR_KEY_WORK_CLI` lands on laptop and the work dir
  sets `MIMIR_MCP_INGRESS=work_cli`. Until then they degrade to personal+shared — fail-safe.

## Verified 2026-07-17

Match ("du…tabby") → 2 correct stamp lines with real timestamps · work entities silent in
personal scope · no-match/short/slash silent · broken API silent rc=0 · no-match latency
50–70ms including interpreter startup.

Disable: remove the entry from `~/.claude/settings.json` (or `/hooks` UI).
