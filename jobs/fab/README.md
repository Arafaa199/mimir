# fab — voice-to-fabrication ("talk → it prints") (Mimir flagship #2)

The first **physical-actuator** capability: describe an object → the system
designs/finds it, slices it, and **on your approval** prints it. Where the trust
engine meets real-world (fire) risk.

Spec: [`mimir/specs/02-3d-print.md`](../../specs/02-3d-print.md).
Status: **v0 LIVE on db-host** (`mimir-fab.service`, tailnet `:8400`) — tested
end-to-end short of the physical print (2026-07-08). One printer
(Ender-3/Moonraker), both gen paths, trust-gated. Functional gen runs on **local
ollama qwen2.5:7b** (worker, swap-backed — this key's OpenRouter is free-tier-only).

## The gated flow (never slice-and-print in one shot)

```
 request ─ classify ─ (ambiguous? ASK) ─ cache? ─┬─ FUNCTIONAL: LLM→OpenSCAD→render
                                                  └─ DECORATIVE: Meshy text→3D
        → DELIVER preview + dimensions (Telegram)  → [approve DESIGN]
        → slice (PrusaSlicer)  → slice summary (time/filament/layers)
        → PROPOSE print  (ops.action_audit decision='proposed')   → [approve START]
        → GATE: printer on? → audit 'executed' → dispatch (HEATS) → printing
        → confirm it fit → model cached as PROVEN (reused next time)
```

## Retrieval-first path (search a repo → print a proven STL)

Generating CAD is a dice-roll (a 7B model put half a "cable clip" below the bed →
spaghetti). Preferred path: **find a print-tested community model** instead.

```
POST /fab/search {query}          → ranked results (real photos + license), DRY
POST /fab/print-from-search       → download STL → grounding/bed-fit guard →
     {provider,thing_id,file_id?}   [same slice→approve→propose→dispatch→Obico]
```

- Provider-abstracted (`search.py`): **Thingiverse** live (`THINGIVERSE_TOKEN`),
  Printables/Kiln pluggable. Ranked by **relevance** (popularity lets a viral
  generic model hijack a specific query). WAF needs a browser `User-Agent`.
- **Grounding + bed-fit guard** (`generate.check_printable`) runs on *every* STL
  (generated or downloaded): rejects models below z=0 or larger than the bed —
  the fix for the spaghetti failure.
- Two human look-points: pick from photo'd results, then approve the sliced design.
- Routing intent: **search first; generate only for exact-fit custom parts** that
  nothing online matches.

## Safety invariants (fire risk — the whole point of the gate)

1. **`start_print` (Moonraker dispatch) happens in exactly one place** —
   `do_approve_print()` — and only after an `ops.action_audit` row moves to
   `decision='executed', approver='user'`. Audit-first, then heat.
2. **`fabrication` trust ceiling = `propose`** (migration 289). `ops.trust_tier`
   can never exceed propose → **there is no auto path**; every print needs an
   explicit `approve-print` call. Raise to `bounded` LATER, only once printer-cam
   monitoring + a home-presence gate exist (spec §3).
3. **`X-Fab-Key` on every endpoint** — only Odin (relaying an explicit user tap)
   can reach the actuator. Bound to the **db-host tailnet IP**, not `0.0.0.0`.
4. **Design + slice are ungated** (files only, no world effect).

## Files

| File | Role |
|---|---|
| `server.py` | HTTP API + gated state machine + delivery (safety-critical) |
| `db.py` | Postgres (jobs, cache, `ops.trust_tier`, `ops.action_audit`) via `docker exec` |
| `generate.py` | classify · LLM→OpenSCAD · Meshy · headless render + STL bbox |
| `fabricate.py` | PrusaSlicer CLI slice · Moonraker upload+start (dispatch) |
| `embed.py` | Ollama embedding for the pgvector cache |
| `fab-config.json` | endpoints, models, thresholds — all tunable, no code change |
| `fab.sh` / `systemd/mimir-fab.service` | wrapper + unit (user service on db-host) |
| `deploy.sh` | provision toolchain + apply migrations + deploy (run when on-LAN) |
| DB | migrations `289_fabrication_trust`, `290_fab_cache` (`fab.*`) |

## Deploy (when the homelab is reachable)

```bash
./deploy.sh            # toolchain + migrations + service + health
# then: drop a slicer profile (profiles/README.md), add MESHY_API_KEY, wire Odin.
```

## Acceptance test (drive the gates with curl; K=$FAB_API_KEY, B=http://localhost:8400)

```bash
# 1. functional request → returns {job_id, status:generating}; preview arrives on Telegram
curl -s $B/fab/request -H "X-Fab-Key: $K" -d '{"prompt":"a 30mm cable clip for a 5mm cable","surface":"text"}'
# 2. approve the DESIGN → slices, returns slice summary, proposes the print
curl -s $B/fab/approve-design -H "X-Fab-Key: $K" -d '{"job_id":"<id>"}'
# 3. power the Ender-3 ON, be present, then approve the START (the gated heat)
curl -s $B/fab/approve-print  -H "X-Fab-Key: $K" -d '{"job_id":"<id>"}'
# 4. once it prints + fits, confirm → caches the model as PROVEN
curl -s $B/fab/confirm -H "X-Fab-Key: $K" -d '{"job_id":"<id>","worked":true}'
# 5. repeat request → served from cache (cache_hit=true), no regeneration
```
Plus one **decorative** part via Meshy (needs `MESHY_API_KEY`). Success = both
complete with human approval at the design + print gates, and the repeat is cached.

## Verified live (2026-07-08)

- ✅ classify → **local qwen2.5:7b** OpenSCAD gen → openscad render (STL + **PNG
  preview** via xvfb + bbox) → approve-design → **PrusaSlicer** slice (time/
  filament/layers parsed) → propose (`ops.action_audit`) → approve-print.
- ✅ **The gate**: approve-print with the Ender-3 off → refused, **no dispatch,
  no 'executed' audit** (only 'proposed'). `ops.trust_tier('fabrication')`='propose'.
- ✅ **pgvector cache_hit**: a reworded repeat retrieved the model, skipped regen.
- ✅ decorative degrades cleanly without `MESHY_API_KEY`; auth (401 w/o key).

## Remaining (external, can't be done remotely)

- **Physical print** — Ender-3 is kept **powered off**; needs power + you present
  (the gated fire-risk act). Everything up to dispatch is proven.
- **Meshy key** — add `MESHY_API_KEY` (free tier at meshy.ai) to `~/.config/fab.env`
  for the decorative path.
- **Odin wiring** — register the fab endpoint (db-host-tailnet `:8400` + `FAB_API_KEY`)
  with the gateway so voice/text routes to it.
- **Inference speed** — swap-backed 7B is ~90s/part; fund an OpenRouter key
  (`provider=openrouter` + a paid model) or an M4 inference node for speed/quality.

## Deploy gotchas hit + fixed

- This key's OpenRouter is **free-tier-only** (paid→402, free→429-throttled) →
  functional gen uses local ollama; added a **4GB swap on worker** so qwen2.5:7b
  (4.7GB) loads (also fixed the plaud/whisper OOM).
- `~` in `slicer.config_ini` must be `expanduser`'d (subprocess has no shell).
- Service binds the **db-host tailnet IP**, not `0.0.0.0`/localhost — curl that IP.

## Why direct backend, not the Kiln MCP (deviation from spec §"the tool")

The spec says use Kiln. I built a **direct** OpenSCAD+PrusaSlicer+Moonraker
backend for v0 instead, because: (a) Kiln is a 838-tool **MCP** designed to be
LLM-facing — exposing it to the model would let the LLM call `start_print`,
bypassing the gate (design laws 3 & 5); the gate must live in *our* service.
(b) It drives fire-risk hardware and couldn't be vetted live off-LAN. (c) Direct
calls to three documented tools are "boring glue" (design law 7). Kiln stays a
drop-in behind `fabricate.py` if we later want its slicing/fleet features —
flag open for your call.

## Obico (monitoring / failure detection)

Obico is self-hosted on db-host (`https://obico.example.internal`, ML API `:3333`) with the
Ender-3 linked (printer id 1). It already does AI print-failure detection + its
own alerting. The fab service hands off its **live monitor URL** in the propose +
printing messages (`obico.monitor_path` in config) — Obico watches independently
(its API is OAuth2; the URL handoff needs no token). Obico is the **"printer-cam
monitoring"** half of the spec §3 prerequisite to later raise the `fabrication`
ceiling `propose → bounded`; the remaining half is a **home-presence check** (HA).

## v1

Voice via glasses (Gemini Live → Odin → fab); second printer (Centauri Carbon 2
is CC2/MQTT — no Moonraker/SDCP, needs the HA `elegoo_printer` path, NOT the
spec's assumed SDCP); **presence check + Obico-watching gate → raise `fabrication`
ceiling to `bounded`**; consume Obico failure signals to auto-pause.
