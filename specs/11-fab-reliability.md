# Spec 11 — fab reliability: word→nozzle, not word→clog

**STATUS: PHASE 1 SHIPPED (2026-08-05, fable-jarvis).** Gates 3/4/5 (`lint.py`,
material windows in fab-config, runs on every slice — errors block the propose,
warnings ride in it), gate 6 (post-dispatch watcher thread + startup stale-sweep;
the sweep closed 4-day-stale job d8d05f82 on its first boot), gate 9 (dispatch
retries, connection-level only). Proof: `tests/slice_suite.py` on db-host — 3
reference STLs repair→slice→lint green + 4 negative lint cases fire (9/9).
Phases 2–4 below remain open; original plan follows.

Owner's ask after the first live day:
"how do we make this reliable so word-to-nozzle isn't word-to-clog."

## Frame

The chain is: intent → model (search/gen) → mesh → slice (profile) → machine →
first layer → mid-print → part verdict. Reliability = every link either passes a
**deterministic gate** or fails **loudly, early, and cheaply** — and the system
**learns from every outcome** (proven-cache up, failure ledger down). LLMs judge
nothing in this chain; scripts gate, the owner arbitrates (design laws 2, 6).

Every gate below is grounded in a failure that actually happened on 2026-07-17/18:
cold-PETG grind, generic-profile bad first layer, "no layers" wild mesh, 0.9g-vs-3.0g
repair ambiguity, Y-endstop connector, Moonraker flaps, smart-plug race, stale
"printing" job rows, and quality verdicts that lived only in the owner's eyes.

## A. Pre-flight gates (before the propose message; all deterministic)

1. **Machine readiness probe** — plug powered (HA `switch.3d_printer_socket_1` +
   power sensor), Moonraker `ready`, Klipper not shutdown, filament runout sensor
   present/ok (`binary_sensor.3d_printer_filament`). Surfaced in the propose
   message; approve-print re-checks (it already gates on reachability).
2. **Mesh verdict, tiered not binary** — watertight (green) / repaired-not-watertight
   (YELLOW: say so in the propose, suggest alternate file) / unsliceable (red, honest
   fail). Already half-built; the tier surfaces in messages.
3. **Slice-volume sanity** — compare gcode filament volume against trimesh mesh
   volume × infill envelope. This makes the 0.9g/3.0g repair-variant problem
   *detectable* instead of lucky. Outside envelope ⇒ warn or deny.
4. **Gcode linter** — parse the emitted gcode: temps inside the material window
   (petg 230–260 / bed 70–85; pla 190–220 / 50–65), first layer ≥0.1mm, no Z<0,
   fits bed, purge present. Runs on every slice forever ⇒ every future profile
   edit is linted for free. A bad edit can never ship 255° PLA.
5. **Material window table** — the material system (v1.1) gains per-material
   temp/fan windows the linter enforces. Loaded-spool mismatch already refuses.

## B. First-layer gate (the industry's biggest single lever)

After layer 1–2 (Moonraker layer progress): camera snapshot
(`camera.ender3v3ke_cam` / Obico) → Telegram: "first layer down — reply stop to
abort." **Notify-only v1** (design law 1: no auto-judgment until the notify loop
has a track record). v2 (later): worker-local vision model advises; auto-pause is
a trust-ratchet decision, not a default.

## C. In-flight

6. **fab watches what it dispatched** — today dispatch is fire-and-forget (stale
   "printing" rows prove it). A watcher thread: poll print state → job status
   transitions → Telegram close-out on complete/error/cancel. Fixes state truth.
7. **Klipper-error triage** — on error state, tail klippy.log → worker-local
   qwen one-liner → Telegram ("Y endstop never triggered — check the connector").
   The key22 experience, automated. LLM *explains*, never acts.
8. **Obico stays the spaghetti watch** (already live). Its pause/flag reaches the
   job via Moonraker pause-state polling (no OAuth wiring needed in v1).
9. **Dispatch retries** — upload/start retry with backoff (Moonraker flaps twice
   a day; a human re-curled it this session, the code should).

## D. Post-flight + the learning loop (the ratchet's fuel)

10. **Completion close-out** — end snapshot + "does it look right / does it fit?"
    confirm yes → proven cache (exists). confirm no → **failure row tagged by
    stage** (first-layer / mid / quality / fit) + material + profile-version.
11. **Reliability ledger** — per (model × material × profile-hash) outcomes in
    fab db. Search ranking then prefers models that have *printed on this machine*
    over Thingiverse hearts; own history beats internet popularity.
12. **Profile versioning + canary** — profile edits are hashed into every job;
    an edit only goes live after the linter passes it against 3 reference STLs
    (cube / whistle / carabiner) within known-good envelopes. Cheap slicing CI;
    no printer needed.

## E. Environment hardening (the boring glue that actually bit us)

13. **HA interlock** — fab sets `input_boolean.fab_print_active` on dispatch,
    clears on close-out; the power-off automations condition on it. Kills the
    plug-race class. (Owner re-enables his automations after this lands.)
14. **Maintenance counter** — grams/hours since last nozzle change in fab db;
    nudge at threshold. Clogs age in; the counter makes "when did you last clean
    it" a query, not a memory.

## F. Deliberately human, unchanged

Design approve (eyeball the preview/photo), print approve (fire), bed-clear,
spool swaps (`fab material petg`). Reliability first; the propose→bounded
autonomy ratchet (presence + Obico + a ledger streak) is a later, separate
decision — spec 02 §3 still owns it.

## Sequencing

- **Phase 1 (no printer needed; one executor session):** gates 3, 4, 5 + 6, 9 +
  stale-job cleanup. Test = offline slice suite on the 3 reference STLs.
- **Phase 2 (one short print):** B (first-layer snapshot), 10 (close-out), 13
  (HA interlock).
- **Phase 3:** 11, 12 (ledger + canary), 1 (readiness probe in propose).
- **Phase 4 (later):** 7 (log triage), vision advice, 14, then the ratchet talk.

Phase 1 is pure deterministic code against artifacts we already have — highest
reliability-per-line in the plan. Start there.
