"""Spec 11 Phase 1 — deterministic gcode gates (no LLM, no printer needed).

lint_gcode() runs on every slice: material temp windows, first-layer height,
bed fit, purge presence, and slice-volume sanity vs the mesh (the 0.9g/3.0g
repair-variant detector). Errors block the propose; warnings ride along in it.
"""

from __future__ import annotations

import math
import re

FILAMENT_AREA_MM2 = math.pi * (1.75 / 2) ** 2  # 2.405 mm^2

_MOVE = re.compile(r"^G[01]\b")
_COORD = re.compile(r"([XYZEF])(-?\d+\.?\d*)")
_TEMP = re.compile(r"^M(104|109|140|190)\b.*?S(\d+\.?\d*)")


def mesh_volume_cm3(stl_path: str) -> float | None:
    """Best-effort mesh volume via trimesh; None if unavailable."""
    try:
        import trimesh  # noqa: PLC0415 — optional, fail-open
        m = trimesh.load(stl_path, force="mesh")
        v = float(m.volume) / 1000.0
        return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _parse(gcode_path: str) -> dict:
    nozzle, bed = [], []
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    z = None
    first_extrude_z = None
    first_layer_z = None
    e_total = 0.0
    e_last = 0.0
    relative_e = False
    pre_layer_e = 0.0
    seen_layer = False

    with open(gcode_path, "r", errors="ignore") as f:
        for raw in f:
            line = raw.split(";", 1)[0].strip()
            if not line:
                if ";LAYER_CHANGE" in raw:
                    seen_layer = True
                continue
            if line.startswith(("M82",)):
                relative_e = False
                continue
            if line.startswith(("M83",)):
                relative_e = True
                continue
            if line.startswith("G92"):
                m = re.search(r"E(-?\d+\.?\d*)", line)
                if m:
                    e_last = float(m.group(1))
                continue
            t = _TEMP.match(line)
            if t:
                code, s = t.group(1), float(t.group(2))
                if s > 0:
                    (nozzle if code in ("104", "109") else bed).append(s)
                continue
            if not _MOVE.match(line):
                continue
            coords = dict((k, float(v)) for k, v in _COORD.findall(line))
            if "Z" in coords:
                z = coords["Z"]
                zmin, zmax = min(zmin, z), max(zmax, z)
            de = 0.0
            if "E" in coords:
                e = coords["E"]
                de = e if relative_e else e - e_last
                if not relative_e:
                    e_last = e
                if de > 0:
                    e_total += de
                    if not seen_layer:
                        pre_layer_e += de
                    elif first_layer_z is None and z is not None:
                        first_layer_z = z
                    if first_extrude_z is None and z is not None:
                        first_extrude_z = z
            if ("X" in coords or "Y" in coords) and de >= 0:
                if "X" in coords:
                    xmin, xmax = min(xmin, coords["X"]), max(xmax, coords["X"])
                if "Y" in coords:
                    ymin, ymax = min(ymin, coords["Y"]), max(ymax, coords["Y"])

    return {"nozzle": nozzle, "bed": bed,
            "x": (xmin, xmax), "y": (ymin, ymax), "z": (zmin, zmax),
            "first_extrude_z": first_extrude_z, "first_layer_z": first_layer_z,
            "e_total_mm": round(e_total, 1), "purge_e_mm": round(pre_layer_e, 1)}


def lint_gcode(gcode_path: str, material: str | None, cfg: dict,
               mesh_vol_cm3: float | None = None) -> dict:
    """Deterministic gate. Returns {ok, errors, warnings, stats}."""
    errors, warnings = [], []
    sl = cfg.get("slicer", {})
    windows = (sl.get("materials") or {}).get((material or "").lower())
    printers = cfg.get("printers") or {}
    first = next(iter(printers.values()), {}) if isinstance(printers, dict) else {}
    bed_mm = first.get("bed_mm", [220, 220, 250])
    margin = sl.get("bed_margin_mm", 5)  # KE purge line runs at X=-2

    p = _parse(gcode_path)

    if not p["nozzle"]:
        errors.append("no nozzle temperature commands found")
    if windows:
        lo, hi = windows["nozzle"]
        bad = [t for t in p["nozzle"] if not lo <= t <= hi]
        if bad:
            errors.append(f"nozzle temp {sorted(set(bad))} outside {material} window [{lo},{hi}]")
        blo, bhi = windows["bed"]
        badb = [t for t in p["bed"] if not blo <= t <= bhi]
        if badb:
            errors.append(f"bed temp {sorted(set(badb))} outside {material} window [{blo},{bhi}]")
    else:
        warnings.append(f"no material window for {material!r} — temp check skipped")

    fz = p["first_layer_z"] or p["first_extrude_z"]
    if fz is None:
        errors.append("no extruding move found")
    elif not 0.1 <= fz <= 0.6:
        errors.append(f"first extrusion at Z={fz} (want 0.1–0.6)")

    if p["z"][0] < -0.01:
        errors.append(f"negative Z move: {p['z'][0]}")
    if p["z"][1] > bed_mm[2]:
        errors.append(f"Z {p['z'][1]} exceeds printer height {bed_mm[2]}")
    if p["x"][0] < -margin or p["x"][1] > bed_mm[0] + 0.01:
        errors.append(f"X range {p['x']} outside bed 0–{bed_mm[0]} (margin {margin})")
    if p["y"][0] < -margin or p["y"][1] > bed_mm[1] + 0.01:
        errors.append(f"Y range {p['y']} outside bed 0–{bed_mm[1]} (margin {margin})")

    if p["purge_e_mm"] < 5:
        warnings.append(f"little/no purge before first layer ({p['purge_e_mm']}mm filament)")

    fil_cm3 = round(p["e_total_mm"] * FILAMENT_AREA_MM2 / 1000.0, 2)
    ratio = None
    if mesh_vol_cm3:
        ratio = round(fil_cm3 / mesh_vol_cm3, 3)
        rlo, rhi = sl.get("volume_ratio", [0.08, 1.35])
        if not rlo <= ratio <= rhi:
            errors.append(
                f"filament volume {fil_cm3}cm³ vs mesh {round(mesh_vol_cm3, 1)}cm³ "
                f"(ratio {ratio}) outside sane envelope [{rlo},{rhi}] — "
                f"repair/slice produced a different part than the mesh")
    else:
        warnings.append("mesh volume unknown — volume sanity skipped")

    stats = {**p, "filament_cm3": fil_cm3, "mesh_cm3": mesh_vol_cm3, "volume_ratio": ratio}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}
