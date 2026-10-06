#!/usr/bin/env python3
"""fab.server — Mimir voice-to-fabrication service (spec 02, v0).

The trust-gated state machine + HTTP API that the Odin gateway calls. One
printer (Ender-3/Moonraker). Both generation paths. The flow:

  request -> classify -> [ambiguous? ASK] -> cache? -> generate -> render preview
          -> DELIVER preview (Telegram) -> await_design_approval
  approve-design -> slice -> slice summary -> PROPOSE print (audit) -> await_print_approval
  approve-print  -> [GATE] printer on? -> audit executed -> dispatch (HEAT) -> printing
  confirm        -> cache the proven model (worked=true)

SAFETY INVARIANTS (fire risk):
  * start_print (Moonraker dispatch) happens ONLY in handle_approve_print, and
    ONLY after an ops.action_audit row is moved to decision='executed'.
  * fabrication trust ceiling = 'propose' => there is no auto path; every print
    requires an explicit approve-print call.
  * All endpoints require X-Fab-Key (shared secret) so only Odin — relaying an
    explicit user tap — can reach the actuator (design law 5: no untrusted act).
  * Design + slice are ungated (files only, no world effect).

Dependency-free (stdlib). Env: FAB_API_KEY, OPENROUTER_API_KEY, MESHY_API_KEY
(optional), TELEGRAM_SHIM_URL.
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import embed
import fabricate
import lint
import generate
import repair
import search

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("FAB_CONFIG", os.path.join(HERE, "fab-config.json"))
FAB_API_KEY = os.environ.get("FAB_API_KEY", "")
SHIM_URL = os.environ.get("TELEGRAM_SHIM_URL", "http://localhost:3340/send")

with open(CONFIG_PATH) as _f:
    CFG = json.load(_f)


def log(m: str) -> None:
    print(f"[fab.server] {m}", file=sys.stderr, flush=True)


def default_printer() -> dict:
    printers = CFG["printers"]
    for name, p in printers.items():
        if p.get("default"):
            return {**p, "name": name}
    name, p = next(iter(printers.items()))
    return {**p, "name": name}


def work_dir(job_id: str) -> str:
    d = os.path.join(os.path.expanduser(CFG.get("work_dir", "~/mimir/fab/work")), job_id)
    os.makedirs(d, exist_ok=True)
    return d


def obico_monitor_url() -> str:
    """Live Obico monitor URL for the printer (Obico's AI watches for failures
    independently). Empty string if Obico is disabled."""
    ob = CFG.get("obico", {})
    if not ob.get("enabled"):
        return ""
    path = ob.get("monitor_path", "/printers/{printer_id}/control/").format(
        printer_id=ob.get("printer_id", 1))
    return ob.get("base_url", "").rstrip("/") + path


# --------------------------------------------------------------------------- #
# Delivery (Telegram via Odin shim)
# --------------------------------------------------------------------------- #
def deliver(text: str) -> bool:
    try:
        body = json.dumps({"message": text}).encode()
        req = urllib.request.Request(SHIM_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 — delivery is best-effort
        log(f"deliver failed: {e}")
        return False


def deliver_photo(png_path: str, caption: str) -> bool:
    try:
        with open(png_path, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode()
        body = json.dumps({"photo_b64": photo_b64, "caption": caption}).encode()
        url = SHIM_URL.rsplit("/", 1)[0] + "/send-photo"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 — delivery is best-effort
        log(f"deliver_photo failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# Stage 1 — request -> generate -> preview (background worker)
# --------------------------------------------------------------------------- #
def process_request(job_id: str, prompt: str, kind: str) -> None:
    """Runs in a worker thread. Cache-check, generate, render, deliver preview."""
    try:
        db.job_update(job_id, kind=kind, status="generating")
        printer = default_printer()

        # cache check (proven model for this obligation)
        vec = embed.embed(prompt, CFG)
        hits = db.cache_search(vec, kind, CFG["cache"]["min_similarity"], prompt) \
            if CFG["cache"].get("enabled", True) else []
        if hits:
            hit = hits[0]
            _deliver_cached(job_id, prompt, kind, hit, printer)
            return

        wd = work_dir(job_id)
        stem = os.path.join(wd, "model")
        if kind == "functional":
            scad = generate.generate_openscad(prompt, CFG)
            rendered = _render_with_retries(scad, stem, prompt)
            db.job_update(job_id, scad_code=rendered["scad_code"],
                          stl_path=rendered["stl_path"], preview_path=rendered["preview_path"],
                          dimensions_mm=rendered["dimensions_mm"],
                          printer=printer["name"], status="awaiting_design_approval")
            dims = rendered["dimensions_mm"]
            _deliver_preview(job_id, prompt, kind,
                             f"{dims['x']}×{dims['y']}×{dims['z']} mm", rendered["preview_path"])
        else:  # decorative
            res = generate.generate_meshy(prompt, stem, CFG)
            db.job_update(job_id, stl_path=res["stl_path"], preview_path=res["preview_path"],
                          dimensions_mm=res["dimensions_mm"], printer=printer["name"],
                          status="awaiting_design_approval")
            d = res["dimensions_mm"]
            _deliver_preview(job_id, prompt, kind,
                             f"{d['x']}×{d['y']}×{d['z']} mm (rescale at slice if needed)",
                             res["preview_path"])
    except Exception as e:  # noqa: BLE001 — surface the failure to the job + user
        log(f"request {job_id} failed: {e}")
        db.job_update(job_id, status="failed", error=str(e)[:800])
        deliver(f"🖨️ Couldn't make that: {str(e)[:200]}")


def _render_with_retries(scad: str, stem: str, prompt: str) -> dict:
    attempts = CFG["generation"].get("max_openscad_attempts", 3)
    err = ""
    for i in range(attempts):
        code = scad if i == 0 else generate.generate_openscad(prompt, CFG, prior_error=err)
        try:
            r = generate.render_openscad(code, stem, CFG)
            return {**r, "scad_code": code}
        except RuntimeError as e:
            err = str(e)
            log(f"OpenSCAD render attempt {i+1}/{attempts} failed: {err[:200]}")
    raise RuntimeError(f"OpenSCAD failed after {attempts} attempts: {err[:300]}")


def _deliver_cached(job_id: str, prompt: str, kind: str, hit: dict, printer: dict) -> None:
    """Serve a proven model from cache — skip regeneration."""
    wd = work_dir(job_id)
    stem = os.path.join(wd, "model")
    if kind == "functional" and hit.get("scad_code"):
        r = generate.render_openscad(hit["scad_code"], stem, CFG)
        db.job_update(job_id, cache_hit=True, cache_model_id=hit["id"],
                      scad_code=hit["scad_code"], stl_path=r["stl_path"],
                      preview_path=r["preview_path"], dimensions_mm=r["dimensions_mm"],
                      printer=printer["name"], status="awaiting_design_approval")
        dims = r["dimensions_mm"]
    else:  # decorative cached STL
        db.job_update(job_id, cache_hit=True, cache_model_id=hit["id"],
                      stl_path=hit.get("stl_path"), dimensions_mm=hit.get("dimensions_mm"),
                      printer=printer["name"], status="awaiting_design_approval")
        dims = hit.get("dimensions_mm") or {"x": "?", "y": "?", "z": "?"}
    deliver(f"🖨️ Found a proven model for “{prompt}” (used {hit.get('print_count',0)}× before).\n"
            f"{dims.get('x')}×{dims.get('y')}×{dims.get('z')} mm · reply to approve the design.\n"
            f"job {job_id}")


def _deliver_preview(job_id: str, prompt: str, kind: str, dims_str: str, preview_path: str) -> None:
    caption = (f"🖨️ Design ready — “{prompt}” [{kind}]\n"
               f"Size: {dims_str}\n"
               f"Approve the design to slice it. job {job_id}")
    if preview_path and os.path.exists(preview_path) and deliver_photo(preview_path, caption):
        return
    deliver(caption + "\n(preview image unavailable — dimensions only)")


def process_retrieval(job_id: str, provider: str, thing_id: str, body: dict) -> None:
    """Worker: download a chosen repo model, grounding/bed-fit check it, then
    park it at awaiting_design_approval with the real product photo as preview."""
    try:
        db.job_update(job_id, kind="retrieved", status="generating")
        printer = default_printer()
        fetched = search.fetch_stl(provider, thing_id, work_dir(job_id), body.get("file_id"))
        mesh_fix = repair.ensure_sliceable(fetched["stl_path"])
        bounds = generate.stl_bounds(fetched["stl_path"])
        generate.check_printable(bounds, printer.get("bed_mm", [220, 220, 250]))
        params = {"origin": "retrieved", "provider": provider, "thing_id": thing_id,
                  "source_url": body.get("page_url", ""), "license": body.get("license", ""),
                  "file_name": fetched["file_name"], "choices": fetched["choices"],
                  "material": body.get("material"), "mesh_repair": mesh_fix}
        db.job_update(job_id, stl_path=fetched["stl_path"],
                      preview_path=body.get("thumbnail", ""), dimensions_mm=bounds["size"],
                      params=params, printer=printer["name"], status="awaiting_design_approval")
        d = bounds["size"]
        extra = f" · {len(fetched['choices'])} STL files (largest picked)" if len(fetched["choices"]) > 1 else ""
        if mesh_fix.get("repaired"):
            extra += " · mesh auto-repaired"
        deliver(f"🔎 Found “{body.get('title', 'model')}” [{provider}]\n"
                f"Size {d['x']}×{d['y']}×{d['z']}mm · {body.get('license') or 'license n/a'}{extra}\n"
                f"{body.get('page_url', '')}\nApprove the design to slice it. job {job_id}")
    except Exception as e:  # noqa: BLE001 — surface the failure to the job + user
        log(f"retrieval {job_id} failed: {e}")
        db.job_update(job_id, status="failed", error=str(e)[:800])
        deliver(f"🖨️ Couldn't fetch/prepare that model: {str(e)[:200]}")


def process_upload(job_id: str, stl_b64: str, body: dict) -> None:
    """Worker: an STL modeled/modified on the CLI seat enters the SAME gated chain as
    retrieval — repair, grounding/bed-fit, design gate, print gate. The preview is an
    OpenSCAD import() render so the human approves what the file actually contains,
    not what the modeler claims it contains."""
    try:
        db.job_update(job_id, kind="uploaded", status="generating")
        printer = default_printer()
        wd = work_dir(job_id)
        stl_path = os.path.join(wd, "model.stl")
        with open(stl_path, "wb") as f:
            f.write(base64.b64decode(stl_b64))
        mesh_fix = repair.ensure_sliceable(stl_path)
        bounds = generate.stl_bounds(stl_path)
        generate.check_printable(bounds, printer.get("bed_mm", [220, 220, 250]))
        preview = ""
        try:
            scad = os.path.join(wd, "preview.scad")
            with open(scad, "w") as f:
                f.write(f'import("{stl_path}");\n')
            preview = os.path.join(wd, "preview.png")
            r = generate.render_preview(scad, preview) if hasattr(generate, "render_preview") else None
            if r is None:
                subprocess_preview(scad, preview)
        except Exception as e:  # noqa: BLE001 — preview is decoration, dims are the contract
            log(f"upload preview failed (dims still shown): {e}")
            preview = ""
        params = {"origin": "uploaded", "source": body.get("source", "cli"),
                  "note": body.get("note", ""), "material": body.get("material"),
                  "mesh_repair": mesh_fix}
        db.job_update(job_id, stl_path=stl_path, preview_path=preview,
                      dimensions_mm=bounds["size"], params=params,
                      printer=printer["name"], status="awaiting_design_approval")
        d = bounds["size"]
        fix_note = " · mesh auto-repaired" if mesh_fix.get("repaired") else ""
        caption = (f"⬆️ Uploaded model “{body.get('title', 'CLI model')}”\n"
                   f"Size {d['x']}×{d['y']}×{d['z']}mm{fix_note}\n"
                   f"Approve the design to slice it. job {job_id}")
        if not (preview and os.path.exists(preview) and deliver_photo(preview, caption)):
            deliver(caption)
    except Exception as e:  # noqa: BLE001
        log(f"upload {job_id} failed: {e}")
        db.job_update(job_id, status="failed", error=str(e)[:800])
        deliver(f"🖨️ Couldn't accept that model: {str(e)[:200]}")


def subprocess_preview(scad_path: str, png_path: str) -> None:
    import subprocess
    cmd = CFG["openscad"].get("render_cmd", ["openscad"]) + [
        "-o", png_path, "--imgsize=900,700",
        "--colorscheme", CFG["openscad"].get("colorscheme", "Tomorrow"), scad_path]
    subprocess.run(cmd, capture_output=True, timeout=CFG["openscad"].get("timeout_s", 120))


# --------------------------------------------------------------------------- #
# Stage 2 — approve-design -> slice -> propose print
# --------------------------------------------------------------------------- #
def do_approve_design(job_id: str) -> dict:
    job = db.job_get(job_id)
    if not job or job["status"] != "awaiting_design_approval":
        return {"error": f"job not awaiting design approval (status={job and job['status']})"}
    db.job_update(job_id, status="slicing")
    material = (job.get("params") or {}).get("material")
    try:
        stem = os.path.join(work_dir(job_id), "model")
        try:
            sliced = fabricate.slice_stl(job["stl_path"], stem, CFG, material=material)
        except RuntimeError as first_err:
            # One repair-then-retry: wild meshes routinely fail "no layers" and
            # trimesh fixes most of them. A mismatch/profile error won't be
            # cured by repair, so only retry when repair actually changed the file.
            fix = repair.ensure_sliceable(job["stl_path"])
            if not fix.get("repaired"):
                raise first_err
            log(f"slice failed, mesh repaired ({fix['note']}), retrying: {job_id}")
            sliced = fabricate.slice_stl(job["stl_path"], stem, CFG, material=material)
    except Exception as e:  # noqa: BLE001
        db.job_update(job_id, status="failed", error=str(e)[:800])
        return {"error": f"slice failed: {e}"}

    summary = sliced["slice_summary"]

    # Spec 11 gates 3/4/5 — deterministic lint on EVERY slice.
    report = lint.lint_gcode(sliced["gcode_path"], sliced.get("material"), CFG,
                             mesh_vol_cm3=lint.mesh_volume_cm3(job["stl_path"]))
    if not report["ok"]:
        problems = "; ".join(report["errors"])[:400]
        db.job_update(job_id, status="failed", error=f"gcode lint: {problems}")
        deliver(f"🛑 Slice REJECTED by the gcode linter — “{job['prompt']}”\n"
                f"{problems}\nNothing was proposed. job {job_id}")
        return {"error": f"lint failed: {problems}", "lint": report, "job_id": job_id}
    lint_note = ""
    if report["warnings"]:
        lint_note = "\n⚠️ " + "; ".join(report["warnings"])[:200]

    printer = default_printer()
    # PROPOSE the print — write the audit row now (decision='proposed').
    audit_id = db.audit_insert(
        agent="mimir-fab", surface=job["surface"], domain=CFG["trust"]["domain"],
        action="start_print",
        params={"job_id": job_id, "prompt": job["prompt"], "printer": printer["name"],
                "slice_summary": summary, "dimensions_mm": job.get("dimensions_mm")},
        decision="proposed", trust_pct_at=db.trust_pct(CFG["trust"]["domain"]),
        tier_at=db.trust_tier(CFG["trust"]["domain"]),
        idempotency_key=f"fab-print-{job_id}")
    params = {**(job.get("params") or {}), "lint": {
        "warnings": report["warnings"], "volume_ratio": report["stats"].get("volume_ratio")}}
    db.job_update(job_id, gcode_path=sliced["gcode_path"], slice_summary=summary,
                  audit_id=audit_id, params=params, status="awaiting_print_approval")

    watch = obico_monitor_url()
    watch_line = f"\n👁 Obico will watch for failures: {watch}" if watch else ""
    deliver(f"🧾 Sliced “{job['prompt']}” [{sliced.get('material', '?').upper()}]\n"
            f"Time {summary.get('print_time') or '?'} · "
            f"Filament {summary.get('filament_g') or '?'}g · "
            f"Layers {summary.get('layers') or '?'} · lint ✓{lint_note}\n"
            f"⚠️ Approving STARTS the printer (heats, runs unattended). "
            f"Only approve if you're home to watch it.{watch_line}\njob {job_id}")
    return {"status": "awaiting_print_approval", "slice_summary": summary, "job_id": job_id}


# --------------------------------------------------------------------------- #
# Stage 3 — approve-print -> GATE -> dispatch (the physical action)
# --------------------------------------------------------------------------- #
def do_approve_print(job_id: str) -> dict:
    job = db.job_get(job_id)
    if not job or job["status"] != "awaiting_print_approval":
        return {"error": f"job not awaiting print approval (status={job and job['status']})"}

    domain = CFG["trust"]["domain"]
    tier = db.trust_tier(domain)          # ALWAYS 'propose' (ceiling) — informational
    tpct = db.trust_pct(domain)
    printer = default_printer()

    # safety: don't dispatch to an unreachable/off printer.
    status = fabricate.moonraker_status(printer)
    if not status.get("reachable"):
        deliver(f"🖨️ Printer {printer['name']} is off/unreachable — power it on, then re-approve. job {job_id}")
        return {"error": "printer offline", "printer_status": status, "job_id": job_id}

    # The physical act. Record the outcome append-only (ops.action_audit is
    # append-only): the 'proposed' row was written at slice-time; here we append
    # 'executed' (or 'failed') — preserving the proposed->executed trail + times.
    try:
        result = fabricate.dispatch(job["gcode_path"], printer)
    except Exception as e:  # noqa: BLE001
        db.audit_insert(agent="mimir-fab", surface=job.get("surface", "text"), domain=domain,
                        action="start_print", params={"job_id": job_id, "printer": printer["name"]},
                        decision="failed", trust_pct_at=tpct, tier_at=tier, approver="user",
                        result={"error": str(e)[:300]})
        db.job_update(job_id, status="failed", error=str(e)[:800])
        deliver(f"🖨️ Print dispatch failed: {str(e)[:200]}. job {job_id}")
        return {"error": f"dispatch failed: {e}"}

    db.audit_insert(agent="mimir-fab", surface=job.get("surface", "text"), domain=domain,
                    action="start_print",
                    params={"job_id": job_id, "printer": printer["name"], "prompt": job["prompt"]},
                    decision="executed", trust_pct_at=tpct, tier_at=tier, approver="user",
                    idempotency_key=f"fab-exec-{job_id}", result={"filename": result.get("filename")})
    _cache_if_new(job)
    params = {**(job.get("params") or {}), "printed_filename": result.get("filename")}
    db.job_update(job_id, params=params, status="printing")
    _spawn_watcher(job_id, printer, result.get("filename"))
    watch = obico_monitor_url()
    watch_line = f"\n👁 Watch (Obico AI monitoring): {watch}" if watch else ""
    deliver(f"🖨️ Printing “{job['prompt']}” on {printer['name']} (tier={tier}).{watch_line}\n"
            f"Reply once it's done so I can remember it worked. job {job_id}")
    return {"status": "printing", "printer": printer["name"], "job_id": job_id,
            "obico_monitor": watch}


# --------------------------------------------------------------------------- #
# Spec 11 gate 6 — fab watches what it dispatched (state truth + close-out)
# --------------------------------------------------------------------------- #
def _spawn_watcher(job_id: str, printer: dict, filename: str) -> None:
    t = threading.Thread(target=_watch_print, args=(job_id, printer, filename),
                         name=f"watch-{job_id[:8]}", daemon=True)
    t.start()


def _watch_print(job_id: str, printer: dict, filename: str) -> None:
    wcfg = CFG.get("watch", {})
    interval = wcfg.get("interval_s", 60)
    grace = wcfg.get("unreachable_grace_s", 1800)
    deadline = time.time() + wcfg.get("max_hours", 24) * 3600
    unreachable_since = None
    time.sleep(30)  # let the start ritual begin
    log(f"watching print {filename} (job {job_id[:8]})")
    while time.time() < deadline:
        job = db.job_get(job_id)
        if not job or job["status"] != "printing":
            return  # closed elsewhere (confirm/deny) — stand down
        st = fabricate.print_stats(printer)
        if not st["reachable"]:
            unreachable_since = unreachable_since or time.time()
            if time.time() - unreachable_since > grace:
                db.job_update(job_id, status="failed",
                              error="printer unreachable mid-print (power cut?)")
                deliver(f"🔌 Lost the printer mid-print for “{job['prompt']}” — unreachable "
                        f"{grace // 60} min (power cut?). Job marked failed. job {job_id}")
                return
        else:
            unreachable_since = None
            state, ours = st["state"], st.get("filename") == filename
            if ours and state == "complete":
                db.job_update(job_id, status="done")
                deliver(f"🏁 Print finished — “{job['prompt']}”.\n"
                        f"Grab it off the bed, then: confirm yes/no so the model's "
                        f"track record updates. job {job_id}")
                return
            if ours and state == "error":
                msg = (st.get("message") or "").strip()[:200]
                db.job_update(job_id, status="failed", error=f"klipper error: {msg or 'unknown'}")
                deliver(f"⚠️ Printer ERROR during “{job['prompt']}”: {msg or 'unknown'}\n"
                        f"job {job_id}")
                return
            if ours and state == "cancelled":
                db.job_update(job_id, status="cancelled")
                deliver(f"⏹ Print cancelled — “{job['prompt']}”. job {job_id}")
                return
            if not ours and st.get("filename"):
                db.job_update(job_id, status="cancelled",
                              error="superseded: printer running a different file")
                deliver(f"⏹ Printer is running a different file — closing job for "
                        f"“{job['prompt']}”. job {job_id}")
                return
        time.sleep(interval)
    log(f"watch timeout for job {job_id[:8]} — leaving status as-is")


def _sweep_stale_printing() -> None:
    """Startup truth-sweep: jobs stuck 'printing' from before a restart either
    get a live watcher re-attached or an honest close-out."""
    try:
        stale = db.jobs_by_status("printing")
    except Exception as e:  # noqa: BLE001
        log(f"stale sweep skipped: {e}")
        return
    for job in stale:
        printer = default_printer()
        fname = (job.get("params") or {}).get("printed_filename", "")
        st = fabricate.print_stats(printer)
        if st["reachable"] and st["state"] == "printing" and st.get("filename") == fname:
            log(f"re-attaching watcher to live print {fname} (job {job['id'][:8]})")
            _spawn_watcher(job["id"], printer, fname)
        elif st["reachable"] and st["state"] == "complete" and st.get("filename") == fname:
            db.job_update(job["id"], status="done")
            log(f"stale job {job['id'][:8]} closed: print completed while fab was down")
        else:
            db.job_update(job["id"], status="cancelled",
                          error="stale: printer no longer printing this job (swept at startup)")
            log(f"stale job {job['id'][:8]} swept (printer state={st.get('state')})")


def _cache_if_new(job: dict) -> None:
    """Insert a freshly-generated model into the cache (worked=NULL until confirmed).
    Cache-hit jobs are already in the cache — just bump usage."""
    if job.get("cache_model_id"):
        db.cache_mark_worked(job["cache_model_id"], True)  # reused a proven model
        return
    if job.get("kind") not in ("functional", "decorative"):
        return  # retrieved/unknown models aren't put in the pgvector gen-cache (v0)
    vec = embed.embed(job["prompt"], CFG)
    db.cache_insert(
        nl_prompt=job["prompt"], kind=job["kind"],
        source_format="openscad" if job["kind"] == "functional" else "meshy",
        scad_code=job.get("scad_code"), stl_path=job.get("stl_path"),
        params=job.get("params") or {}, dimensions_mm=job.get("dimensions_mm"),
        printer=job.get("printer"), slicer_profile=CFG["slicer"].get("config_ini"),
        embedding=vec, worked=None)


def do_confirm(job_id: str, worked: bool) -> dict:
    """User confirms the print finished + fit — promote the model to proven."""
    job = db.job_get(job_id)
    if not job:
        return {"error": "no such job"}
    # find the cache row for this obligation and mark worked
    vec = embed.embed(job["prompt"], CFG)
    hits = db.cache_search(vec, job["kind"], 0.6, job["prompt"], limit=1)
    if hits:
        db.cache_mark_worked(hits[0]["id"], worked)
    db.job_update(job_id, status="done" if worked else "failed")
    return {"status": "done" if worked else "failed", "worked": worked}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter
        log("%s - %s" % (self.address_string(), a[0] % a[1:]))

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not FAB_API_KEY:
            return True  # unset => open (warned at startup)
        if self.headers.get("X-Fab-Key", "") == FAB_API_KEY:
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "mimir-fab", "printer": default_printer()["name"],
                             "loaded_material": fabricate.loaded_material(CFG)})
            return
        if not self._auth():
            return
        if self.path == "/fab/material":
            self._send(200, {"loaded": fabricate.loaded_material(CFG),
                             "profiles": sorted((CFG["slicer"].get("profiles") or {}).keys())})
            return
        if self.path.startswith("/fab/job/"):
            job = db.job_get(self.path.rsplit("/", 1)[-1])
            self._send(200 if job else 404, job or {"error": "not found"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        body = self._body()
        try:
            if self.path == "/fab/search":
                self._send(*self._search(body)); return
            if self.path == "/fab/submit-stl":
                self._send(*self._submit_stl(body)); return
            if self.path == "/fab/print-from-search":
                self._send(*self._print_from_search(body)); return
            if self.path == "/fab/request":
                self._send(*self._request(body)); return
            if self.path == "/fab/approve-design":
                self._send(200, do_approve_design(body.get("job_id", ""))); return
            if self.path == "/fab/approve-print":
                self._send(200, do_approve_print(body.get("job_id", ""))); return
            if self.path == "/fab/deny":
                self._send(200, self._deny(body.get("job_id", ""))); return
            if self.path == "/fab/confirm":
                self._send(200, do_confirm(body.get("job_id", ""), bool(body.get("worked", True)))); return
            if self.path == "/fab/material":
                self._send(*self._set_material(body)); return
        except Exception as e:  # noqa: BLE001
            log(f"handler error: {e}")
            self._send(500, {"error": str(e)[:300]}); return
        self._send(404, {"error": "not found"})

    def _set_material(self, body: dict) -> tuple[int, dict]:
        """Owner swapped the spool. The state file is what resolve_profile trusts,
        so this is the ONLY way a request changes which profile slices."""
        mat = (body.get("material") or "").strip().lower()
        profiles = CFG["slicer"].get("profiles") or {}
        if mat not in profiles:
            return 400, {"error": f"unknown material {mat!r} — profiles exist for: "
                                  f"{', '.join(sorted(profiles)) or 'none'}"}
        fabricate.set_loaded_material(CFG, mat)
        log(f"loaded material set to {mat}")
        return 200, {"status": "ok", "loaded": mat}

    def _check_material(self, body: dict) -> str | None:
        """400 early on an unknown material override (None = fine)."""
        mat = (body.get("material") or "").strip().lower()
        if not mat:
            body.pop("material", None)
            return None
        profiles = CFG["slicer"].get("profiles") or {}
        if mat not in profiles:
            return f"unknown material {mat!r} — profiles exist for: {', '.join(sorted(profiles)) or 'none'}"
        body["material"] = mat
        return None

    def _submit_stl(self, body: dict) -> tuple[int, dict]:
        """CLI-modeled geometry in, same gates out. Size-capped: a 30MB mesh is a
        modeling smell, not a print candidate."""
        stl_b64 = body.get("stl_b64") or ""
        if not stl_b64:
            return 400, {"error": "stl_b64 required"}
        if len(stl_b64) > 40_000_000:
            return 400, {"error": "STL too large (>~30MB decoded)"}
        bad = self._check_material(body)
        if bad:
            return 400, {"error": bad}
        title = body.get("title") or "CLI model"
        job_id = db.job_create(title, body.get("surface", "cli"))
        threading.Thread(target=process_upload,
                         args=(job_id, stl_b64, body), daemon=True).start()
        return 202, {"status": "processing", "job_id": job_id}

    def _search(self, body: dict) -> tuple[int, dict]:
        """Dry retrieval: search repos for proven printable models. No download,
        no print — returns ranked results with real photos so the human can eyeball
        matches before anything goes near the nozzle."""
        query = (body.get("query") or body.get("prompt") or "").strip()
        if not query:
            return 400, {"error": "query required"}
        if not CFG.get("retrieval", {}).get("enabled"):
            return 200, {"status": "disabled", "results": []}
        results = search.search_all(query, CFG, body.get("limit"))
        return 200, {"status": "ok", "query": query, "count": len(results),
                     "providers": search.enabled_providers(CFG), "results": results}

    def _print_from_search(self, body: dict) -> tuple[int, dict]:
        """Take a chosen search result → download its STL → grounding/bed-fit
        check → hand to the SAME slice→approve→print pipeline. The real product
        photo becomes the design-approval preview."""
        provider = body.get("provider", "thingiverse")
        thing_id = str(body.get("thing_id") or body.get("id") or "").strip()
        if not thing_id:
            return 400, {"error": "thing_id (or id) required"}
        bad = self._check_material(body)
        if bad:
            return 400, {"error": bad}
        title = body.get("title") or f"{provider} thing:{thing_id}"
        job_id = db.job_create(title, body.get("surface", "text"))
        threading.Thread(target=process_retrieval,
                         args=(job_id, provider, thing_id, body), daemon=True).start()
        return 202, {"status": "downloading", "job_id": job_id}

    def _request(self, body: dict) -> tuple[int, dict]:
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return 400, {"error": "prompt required"}
        bad = self._check_material(body)
        if bad:
            return 400, {"error": bad}
        surface = body.get("surface", "text")
        kind = body.get("kind") or generate.classify(prompt, CFG)
        if kind == "ambiguous":
            return 200, {"status": "clarify",
                         "question": "Is this FUNCTIONAL (must fit/measure) or DECORATIVE?"}
        job_id = db.job_create(prompt, surface)
        if body.get("material"):
            db.job_update(job_id, params={"material": body["material"]})
        threading.Thread(target=process_request, args=(job_id, prompt, kind), daemon=True).start()
        return 202, {"status": "generating", "kind": kind, "job_id": job_id}

    def _deny(self, job_id: str) -> dict:
        job = db.job_get(job_id)
        if job:
            domain = CFG["trust"]["domain"]
            db.audit_insert(agent="mimir-fab", surface=job.get("surface", "text"),
                            domain=domain, action="start_print", params={"job_id": job_id},
                            decision="denied", trust_pct_at=db.trust_pct(domain),
                            tier_at=db.trust_tier(domain), approver="user")
            db.job_update(job_id, status="denied")
        return {"status": "denied", "job_id": job_id}


def main() -> int:
    if not FAB_API_KEY:
        log("WARNING: FAB_API_KEY not set — endpoints are OPEN. Set it for the fire-risk actuator.")
    listen = CFG.get("listen", {"host": "127.0.0.1", "port": 8400})
    srv = ThreadingHTTPServer((listen["host"], listen["port"]), Handler)
    threading.Thread(target=_sweep_stale_printing, name="stale-sweep", daemon=True).start()
    log(f"listening on {listen['host']}:{listen['port']} — printer={default_printer()['name']}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
