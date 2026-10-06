#!/usr/bin/env python3
"""fab.fabricate — the mechanical/printer backend (slice + dispatch).

slice_stl()        STL  -> PrusaSlicer CLI -> gcode + slice summary  (ungated compute)
moonraker_status() printer reachable / state (502 => powered off)
dispatch()         upload gcode + START print on Moonraker  (THE gated physical act)

v0 target: Creality Ender-3 V3 KE @ localhost, Moonraker :7125 (Klipper).
Direct backend (OpenSCAD + PrusaSlicer + Moonraker) — chosen over the Kiln MCP
for v0 so the trust gate stays entirely in this service and nothing LLM-facing
can reach raw print dispatch (design laws 3 & 5). Kiln remains a drop-in
alternative behind the same functions.

LIVE-VERIFY surfaces (can't be tested off-LAN): exact PrusaSlicer CLI flags for
the installed version, the slice-summary comment format, Moonraker auth mode
(trusted-LAN vs X-Api-Key), and the print/start endpoint shape.
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def _log(m: str) -> None:
    print(f"[fab.fabricate] {m}", flush=True)


# --------------------------------------------------------------------------- #
# Slicing (ungated — pure computation, files only)
# --------------------------------------------------------------------------- #
def loaded_material(cfg: dict) -> str | None:
    """The filament physically in the printer, from the state file (flipped via
    /fab/material when the owner swaps spools). None if never set."""
    state = os.path.expanduser(cfg["slicer"].get("material_state", "~/mimir/fab/.loaded_material"))
    try:
        with open(state) as f:
            return f.read().strip().lower() or None
    except OSError:
        return None


def set_loaded_material(cfg: dict, material: str) -> None:
    state = os.path.expanduser(cfg["slicer"].get("material_state", "~/mimir/fab/.loaded_material"))
    with open(state, "w") as f:
        f.write(material.strip().lower() + "\n")


def resolve_profile(cfg: dict, material: str | None) -> tuple[str, str]:
    """(config_ini, material). Precedence: explicit request > loaded-state file >
    legacy single config_ini. An explicit request that CONTRADICTS the loaded
    spool is refused — slicing PETG with a PLA profile ground the extruder for a
    whole evening; the constraint beats the convenience."""
    sl = cfg["slicer"]
    profiles = sl.get("profiles") or {}
    loaded = loaded_material(cfg)
    want = (material or "").strip().lower() or None
    if want and loaded and want != loaded:
        raise RuntimeError(
            f"material mismatch: requested {want} but loaded filament is {loaded} — "
            f"swap the spool and flip it (fab material {want}), or drop the override")
    mat = want or loaded
    if mat and profiles.get(mat):
        return os.path.expanduser(profiles[mat]), mat
    return os.path.expanduser(sl["config_ini"]), mat or "default"


def slice_stl(stl_path: str, out_stem: str, cfg: dict, material: str | None = None) -> dict:
    """Slice an STL to gcode via PrusaSlicer CLI. Returns
    {gcode_path, material, slice_summary:{print_time, filament_g, filament_m, layers}}."""
    sl = cfg["slicer"]
    gcode_path = out_stem + ".gcode"
    config_ini, mat = resolve_profile(cfg, material)  # subprocess won't expand ~
    cmd = [sl.get("bin", "prusa-slicer"), "--export-gcode",
           "--load", config_ini, "--output", gcode_path]
    cmd += sl.get("extra_args", [])
    cmd.append(stl_path)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=sl.get("timeout_s", 300))
    if proc.returncode != 0:
        raise RuntimeError(f"slice failed: {(proc.stderr or proc.stdout).strip()[:600]}")
    return {"gcode_path": gcode_path, "material": mat,
            "slice_summary": parse_slice_summary(gcode_path)}


def parse_slice_summary(gcode_path: str) -> dict:
    """Pull time/filament/layers from PrusaSlicer's gcode comment header/footer."""
    summary = {"print_time": None, "filament_g": None, "filament_m": None, "layers": None}
    try:
        with open(gcode_path, "r", errors="ignore") as f:
            text = f.read()
    except OSError:
        return summary
    patterns = {
        "print_time": r"estimated printing time \(normal mode\)\s*=\s*(.+)",
        "filament_g": r"filament used \[g\]\s*=\s*([\d.]+)",
        "filament_m": r"filament used \[mm\]\s*=\s*([\d.]+)",
        "layers":     r"total layer(?:s count|number)\s*=\s*(\d+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if key == "filament_m":
                try:
                    val = round(float(val) / 1000.0, 2)  # mm -> m
                except ValueError:
                    pass
            elif key in ("filament_g",):
                try:
                    val = round(float(val), 1)
                except ValueError:
                    pass
            elif key == "layers":
                val = int(val)
            summary[key] = val
    if summary["layers"] is None:
        summary["layers"] = text.count(";LAYER_CHANGE") or None
    return summary


# --------------------------------------------------------------------------- #
# Moonraker HTTP
# --------------------------------------------------------------------------- #
def _mr_headers(printer: dict) -> dict:
    h = {}
    if printer.get("api_key"):
        h["X-Api-Key"] = printer["api_key"]
    return h


def moonraker_status(printer: dict, timeout: int = 6) -> dict:
    """Return {reachable, state}. 502/refused/timeout => powered off."""
    base = printer["base_url"].rstrip("/")
    try:
        req = urllib.request.Request(base + "/printer/info", headers=_mr_headers(printer))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode())
        state = (info.get("result") or {}).get("state", "unknown")
        return {"reachable": True, "state": state}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"reachable": False, "state": "offline", "error": str(e)}


def _retry(what: str, fn, tries: int = 3, delays: tuple = (2, 6, 15)):
    """Retry connection-level failures (Moonraker flaps). HTTP-level errors that
    carry a response (already-printing etc.) are NOT retried — they're real."""
    last = None
    for attempt in range(tries):
        try:
            return fn()
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise  # server answered — retrying won't change its mind
            last = e
            if attempt < tries - 1:
                _log(f"{what} attempt {attempt + 1} failed ({e}); retrying")
                time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"{what} failed after {tries} attempts: {last}")


def dispatch(gcode_path: str, printer: dict) -> dict:
    """THE gated physical action: upload the gcode + START the print.
    Caller MUST have written an approved ops.action_audit row first.
    Returns {ok, filename, response} or raises."""
    base = printer["base_url"].rstrip("/")
    filename = f"mimir_{uuid.uuid4().hex[:8]}.gcode"

    # 1) upload (multipart/form-data, stdlib) — no print yet
    with open(gcode_path, "rb") as f:
        gcode_bytes = f.read()
    body, content_type = _multipart({"root": "gcodes"},
                                    {"file": (filename, gcode_bytes)})

    def _upload():
        up = urllib.request.Request(base + "/server/files/upload", data=body,
                                    headers={**_mr_headers(printer), "Content-Type": content_type})
        with urllib.request.urlopen(up, timeout=60) as resp:
            return json.loads(resp.read().decode())

    up_out = _retry("gcode upload", _upload)

    # 2) START print (the moment the printer heats)
    def _start():
        start = urllib.request.Request(
            base + "/printer/print/start",
            data=json.dumps({"filename": filename}).encode(),
            headers={**_mr_headers(printer), "Content-Type": "application/json"})
        with urllib.request.urlopen(start, timeout=30) as resp:
            return json.loads(resp.read().decode())

    start_out = _retry("print start", _start)
    _log(f"print started: {filename}")
    return {"ok": True, "filename": filename, "upload": up_out, "start": start_out}


def print_stats(printer: dict, timeout: int = 6) -> dict:
    """Poll Moonraker print state: {reachable, state, filename, progress}."""
    base = printer["base_url"].rstrip("/")
    url = base + "/printer/objects/query?print_stats&display_status"
    try:
        req = urllib.request.Request(url, headers=_mr_headers(printer))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            st = json.loads(resp.read().decode())["result"]["status"]
        ps = st.get("print_stats", {})
        return {"reachable": True, "state": ps.get("state", "unknown"),
                "filename": ps.get("filename", ""),
                "message": ps.get("message", ""),
                "progress": round((st.get("display_status", {}).get("progress") or 0) * 100)}
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        return {"reachable": False, "state": "offline", "error": str(e)}


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder (stdlib-only)."""
    boundary = "----mimirfab" + uuid.uuid4().hex
    out = bytearray()
    for name, val in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode()
    for name, (fname, content) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
