#!/usr/bin/env python3
"""fab.repair — best-effort mesh repair for wild STLs.

Thingiverse-class meshes are routinely non-manifold: the first live retrieval
failure was PrusaSlicer's "No layers were detected" on a 6,840-♥ model
(thing 165120). The ladder here is deliberately shallow: trimesh's cheap fixes
(merge/degenerate cleanup, normals, hole fill), then hand the verdict back to
the slicer — the SLICER stays the final arbiter of printability.

No convex-hull or voxel-remesh fallback ON PURPOSE: those silently change
geometry, and a silently different part is worse than an honest failure
(same reasoning as the grounding guard).

Fail-open: if trimesh isn't installed, repair is a no-op and slicing behaves
exactly as before. The original mesh is kept beside the repaired one (.orig).
"""

import os
import shutil


def _log(m: str) -> None:
    print(f"[fab.repair] {m}", flush=True)


def ensure_sliceable(stl_path: str) -> dict:
    """Best-effort in-place repair. Returns {repaired, watertight, note}."""
    try:
        import trimesh
        import trimesh.repair
    except ImportError:
        return {"repaired": False, "watertight": None, "note": "trimesh not installed"}
    try:
        mesh = trimesh.load(stl_path, force="mesh")
    except Exception as e:  # noqa: BLE001 — a broken file must not kill the job here
        return {"repaired": False, "watertight": None, "note": f"load failed: {e}"[:200]}
    if getattr(mesh, "is_watertight", False):
        return {"repaired": False, "watertight": True, "note": "already watertight"}
    orig = stl_path + ".orig"
    if not os.path.exists(orig):
        shutil.copy2(stl_path, orig)
    try:
        mesh.process(validate=True)
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fill_holes(mesh)
        mesh.export(stl_path)
    except Exception as e:  # noqa: BLE001 — restore the original, report honestly
        shutil.copy2(orig, stl_path)
        return {"repaired": False, "watertight": False, "note": f"repair failed: {e}"[:200]}
    watertight = bool(mesh.is_watertight)
    _log(f"repaired {os.path.basename(stl_path)} watertight={watertight}")
    return {"repaired": True, "watertight": watertight,
            "note": "trimesh repair applied" + ("" if watertight else " (still not watertight — slicer decides)")}
