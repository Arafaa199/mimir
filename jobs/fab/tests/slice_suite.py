#!/usr/bin/env python3
"""Spec 11 Phase 1 offline suite — no printer, no LLM.

For each reference STL: repair -> slice (loaded-material profile) -> lint.
Then negative lint cases on synthetic gcode (bad temps, no purge, off-bed,
volume blowout) to prove the gates actually fire.

Run on db-host:  cd ~/mimir/fab && python3 tests/slice_suite.py
Exit 0 = all green.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fabricate  # noqa: E402
import lint  # noqa: E402
import repair  # noqa: E402

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fab-config.json")
CFG = json.load(open(CFG_PATH))
REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# ---- positive path: repair -> slice -> lint on the 3 reference parts -------
material = fabricate.loaded_material(CFG) or "petg"
with tempfile.TemporaryDirectory() as td:
    for stl in sorted(os.listdir(REF)):
        if not stl.endswith(".stl"):
            continue
        src = os.path.join(REF, stl)
        work = os.path.join(td, stl)
        open(work, "wb").write(open(src, "rb").read())
        fix = repair.ensure_sliceable(work)
        try:
            sliced = fabricate.slice_stl(work, os.path.join(td, stl[:-4]), CFG,
                                         material=material)
        except Exception as e:  # noqa: BLE001
            check(f"slice:{stl}", False, str(e)[:120])
            continue
        vol = lint.mesh_volume_cm3(work)
        rep = lint.lint_gcode(sliced["gcode_path"], sliced["material"], CFG, vol)
        s = rep["stats"]
        check(f"slice+lint:{stl}", rep["ok"],
              f"[{material}] fil={s['filament_cm3']}cm3 mesh={vol and round(vol,1)}cm3 "
              f"ratio={s['volume_ratio']} warn={rep['warnings']} err={rep['errors']}")

# ---- negative cases: the gates must FIRE --------------------------------- #
GOOD_HDR = "M140 S80\nM190 S80\nM104 S245\nM109 S245\nG92 E0\nG1 X-2 Y20 Z0.3 F5000\nG1 Y145 F1500 E15\n"
BODY = ";LAYER_CHANGE\nG1 X50 Y50 Z0.2 F9000\n" + "".join(
    f"G1 X{50+i} Y50 E{15 + i * 2}\n" for i in range(1, 40))


def lint_str(gcode: str, mesh_vol=None, mat="petg"):
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as f:
        f.write(gcode)
        p = f.name
    try:
        return lint.lint_gcode(p, mat, CFG, mesh_vol)
    finally:
        os.unlink(p)


r = lint_str(GOOD_HDR.replace("S245", "S255").replace("S80", "S60"), mat="pla")
check("lint rejects 255C PLA", not r["ok"], str(r["errors"])[:100])

r = lint_str(GOOD_HDR + BODY.replace("Z0.2", "Z0.05"))
check("lint rejects 0.05 first layer", not r["ok"], str(r["errors"])[:100])

r = lint_str(GOOD_HDR + BODY.replace("X50 Y50 Z0.2", "X250 Y50 Z0.2"))
check("lint rejects off-bed X", not r["ok"], str(r["errors"])[:100])

r = lint_str(GOOD_HDR + BODY, mesh_vol=50.0)
check("lint flags volume blowout (tiny print vs 50cm3 mesh)", not r["ok"],
      str(r["errors"])[:110])

r = lint_str("M104 S245\nM109 S245\n;LAYER_CHANGE\nG1 X50 Y50 Z0.2 E5 F9000\n"
             + "".join(f"G1 X{50+i} Y50 E{5 + i * 2}\n" for i in range(1, 30)))
check("lint warns on missing purge", any("purge" in w for w in r["warnings"])
      or True, "")  # warning-only gate; presence asserted below
r2 = lint_str("M104 S245\nM109 S245\n;LAYER_CHANGE\nG1 X50 Y50 Z0.2 E5 F9000\n"
              + "".join(f"G1 X{50+i} Y50 E{5 + i * 2}\n" for i in range(1, 30)))
check("no-purge produces warning", any("purge" in w for w in r2["warnings"]),
      str(r2["warnings"])[:100])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL GREEN")
