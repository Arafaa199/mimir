# Spec 02 — Voice-to-Fabrication ("talk → it prints")

**STATUS: ready for executor (Opus/Codex).** Judgments MADE — implement; flag only if reality contradicts. Build in parallel with the cracks-brief 7-day test; they're independent.

## Purpose
Describe an object in natural language (voice via glasses/Telegram, or text) → the system designs or finds it, slices it, and — on your approval — prints it on the Ender-3 or Elegoo Centauri. The "itch" capability, and the **first physical-actuator capability** — so it's where the trust engine meets real-world (fire) risk.

## The tool
**Kiln** (github.com/codeofaxel/Kiln, `pip install kiln3d`) — MCP server chaining NL→geometry→validate→headless slice→printer dispatch. Natively supports Ender-3 (Moonraker/Marlin) + Elegoo Centauri (SDCP WebSocket). Use it; don't rebuild the pipeline.

## Core judgments (architect calls — don't reopen)
1. **Two generation paths, routed per request:**
   - FUNCTIONAL (must fit/measure — bracket, mount, clip, spacer) → LLM→**OpenSCAD** (parametric, dimensioned). The only path that reliably yields parts that fit. Simple geometry only; expect iteration.
   - DECORATIVE/ORGANIC (figurine, topper, ornament) → text/image→mesh via **Meshy/Gemini**. Reliable for shape, NOT dimensions.
   - A cheap-LLM classifier routes it; when ambiguous, ASK "functional or decorative?" — getting this wrong wastes filament and hours.
2. **Preview-before-print is MANDATORY.** LLM→CAD is imperfect. Flow: generate → render a preview image (+ bounding box/dimensions for functional) → deliver to you (Telegram) → you approve the DESIGN → slice → show slice summary (time, filament, layers) → **propose the print** → you approve the START → printer heats. Never slice-and-print in one unapproved shot.
3. **Starting a print is a gated PHYSICAL action.** A printer runs hot, unattended, for hours = the irreversible/fire-risk tier. New trust domain **`fabrication`**, ceiling = **propose** for v0: `start_print` ALWAYS requires approval regardless of trust %. Design + slice are pure computation (files, no world effect) → NOT gated. Only `start_print` writes `ops.action_audit` + requires approval. Ceiling may rise to `bounded` LATER, only once (a) printer-cam monitoring is wired and (b) a home-presence check gates it. Not before.
4. **Cache proven models in pgvector.** Every successful `.scad`/STL (with its NL prompt + "worked: yes") is embedded and stored. "Print another cable clip" retrieves the proven model and skips regeneration — cheaper, faster, no dice-roll. This is the build-on-top improvement over the viral demos.

## Scope
- **v0:** text/Telegram input; both gen paths; preview→approve→slice→propose→approve→print; ONE printer first (the more reachable — likely Ender-3/Moonraker); pgvector cache; `fabrication` domain gated at propose.
- **v1:** voice via glasses (Gemini Live → Odin → fab); both printers with auto-select (FDM vs Centauri by part type); printer-cam + presence check → raise ceiling to bounded; print-failure detection.

## Architecture & host
- Host on a **home-LAN, personal-tailnet, always-on** node — must reach the printers (home LAN) AND run OpenSCAD + a slicer (PrusaSlicer/OrcaSlicer CLI). **Recommend db-host** (always-on, on-LAN, personal tailnet, enough for occasional simple-part slicing). **NOT laptop** (work tailnet can't reach home printers off-LAN; laptop sleeps). If Kiln-as-stdio-MCP under the Odin gateway is simpler, worker works but it's saturated — prefer db-host.
- Integration: expose fab as an HTTP endpoint the **Odin gateway** calls (Odin already calls tailnet HTTP services like tw-api / db-host-db-api). Voice/text → Odin agent → fab → Kiln.

## Trust / action integration
- Executor: add `fabrication` to `ops.trust_domain` via a small migration — `INSERT ... ('fabrication','3D print start — physical/fire risk, attended only','propose') ON CONFLICT DO NOTHING`, plus a 0% baseline row. Design/slice are not gated.
- `start_print` logs `ops.action_audit(domain='fabrication', decision='proposed' → 'executed', approver='user')`.

## Acceptance test
Real end-to-end: type "a 30mm cable clip for a 5mm cable" → get a preview → approve → get a slice summary → approve → it prints and fits. AND one decorative part via Meshy. Success = both complete with human approval at the design and print gates, and a repeat request is served from cache without regenerating.

## Executor decides (impl, not judgment)
Kiln install/config, exact printer endpoints + tokens, classifier prompt, preview-render mechanism, Telegram delivery of preview + approve buttons, pgvector cache table + embedding, host provisioning (OpenSCAD + slicer). Reuse Odin comms + the `ops.action_audit` pattern from migration 287.

## Verify first
- Ender-3: Moonraker vs OctoPrint? exact host/IP/API key (substrate notes: Moonraker via HA).
- Elegoo Centauri: SDCP WebSocket endpoint + auth on the LAN.
- Confirm the chosen host can run OpenSCAD + a slicer CLI and reach both printer IPs; fall back to worker if db-host can't.
- Meshy (or Gemini image-to-3D) API key for the decorative path.
