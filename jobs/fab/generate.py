#!/usr/bin/env python3
"""fab.generate — routing + the two generation paths + preview render.

FUNCTIONAL (must fit/measure)  -> LLM -> parametric OpenSCAD -> render + bbox
DECORATIVE (figurine/ornament) -> Meshy text-to-3D -> STL + thumbnail

A cheap LLM classifies; when genuinely ambiguous we return 'ambiguous' so the
flow ASKS the user (getting this wrong wastes filament + hours — spec §1).

External calls use stdlib urllib; OpenSCAD/STL via subprocess. Env:
OPENROUTER_API_KEY (classify + OpenSCAD gen), MESHY_API_KEY (decorative).

NOTE (live-verify): headless OpenSCAD PNG export needs GL — default command
wraps `xvfb-run`; adjust in config if the host uses OSMesa or a real display.
"""

import json
import os
import struct
import subprocess
import time
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _chat(cfg: dict, model: str, system: str, user: str, temperature: float = 0.2,
          max_tokens: int = 1500) -> str:
    """Route an LLM chat to the configured provider. Default 'ollama' (local,
    no rate limits, private — hybrid-inference principle); 'openrouter' for
    free/hosted models (retries 429s from the shared free pool)."""
    provider = cfg["generation"].get("provider", "ollama")
    if provider == "ollama":
        return _ollama_chat(cfg, model, system, user, temperature, max_tokens)
    return _openrouter_chat(model, system, user, temperature, max_tokens)


def _ollama_chat(cfg: dict, model: str, system: str, user: str,
                 temperature: float, max_tokens: int) -> str:
    url = cfg["generation"].get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/chat"
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg["generation"].get("ollama_timeout_s", 180)) as resp:
        out = json.loads(resp.read().decode())
    return (out.get("message") or {}).get("content", "").strip()

# Deterministic hints — cheap pre-filter before the LLM, and the fallback.
_FUNCTIONAL_HINTS = ("bracket", "mount", "clip", "holder", "spacer", "adapter",
                     "case", "enclosure", "stand", "hook", "jig", "fixture",
                     "gauge", "washer", "insert", "grommet", "bushing", "mm",
                     "millimet", "fit", "diameter", "thread", "hinge", "gear")
_DECORATIVE_HINTS = ("figurine", "figure", "statue", "ornament", "topper",
                     "miniature", "mini", "sculpt", "toy", "character", "dragon",
                     "animal", "bust", "decoration", "decorative", "vase", "planter")


def _log(m: str) -> None:
    print(f"[fab.generate] {m}", flush=True)


def _openrouter_chat(model: str, system: str, user: str, temperature: float = 0.2,
                     max_tokens: int = 1500, retries: int = 3) -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                out = json.loads(resp.read().decode())
            return out["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            # free-pool models are rate-limited (429) — honour Retry-After and retry
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", "20") or 20)
                _log(f"OpenRouter 429 — retrying in {wait}s")
                time.sleep(min(wait, 30))
                continue
            raise
    raise RuntimeError("OpenRouter exhausted retries")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(prompt: str, cfg: dict) -> str:
    """Return 'functional' | 'decorative' | 'ambiguous'."""
    p = prompt.lower()
    f_hit = any(h in p for h in _FUNCTIONAL_HINTS)
    d_hit = any(h in p for h in _DECORATIVE_HINTS)
    if f_hit and not d_hit:
        return "functional"
    if d_hit and not f_hit:
        return "decorative"
    # ask a cheap LLM to break the tie
    try:
        ans = _chat(
            cfg, cfg["generation"]["classifier_model"],
            "You classify a 3D-print request as exactly one word: 'functional' "
            "(a part that must fit or measure — bracket, mount, clip, spacer, "
            "adapter, case) or 'decorative' (a figurine, ornament, topper, toy). "
            "If it is genuinely unclear which, answer 'ambiguous'. One word only.",
            prompt, temperature=0.0, max_tokens=4,
        ).lower()
        for label in ("functional", "decorative", "ambiguous"):
            if label in ans:
                return label
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, KeyError) as e:
        _log(f"classifier LLM unavailable ({e}) — using keyword fallback")
    if f_hit:
        return "functional"
    if d_hit:
        return "decorative"
    return "ambiguous"


# --------------------------------------------------------------------------- #
# FUNCTIONAL path — LLM -> OpenSCAD
# --------------------------------------------------------------------------- #
_SCAD_SYSTEM = (
    "You are a parametric CAD engineer. Output ONLY valid OpenSCAD code for the "
    "requested FUNCTIONAL part — no prose, no markdown fences. Rules: put all "
    "dimensions in NAMED parameters at the top with millimeter comments; honour "
    "every measurement the user gave EXACTLY; add sensible clearances (0.2-0.4mm) "
    "for mating fits; keep geometry simple and printable (FDM, no supports if "
    "avoidable, flat base). Use $fn=64 for curves. The model must be a single "
    "solid centred near the origin sitting on the XY plane (z>=0)."
)


def generate_openscad(prompt: str, cfg: dict, prior_error: str = "") -> str:
    """Return OpenSCAD source for a functional part."""
    user = prompt if not prior_error else (
        f"{prompt}\n\nThe previous attempt failed to render with this error, fix it:\n{prior_error}")
    code = _chat(cfg, cfg["generation"]["openscad_model"], _SCAD_SYSTEM, user,
                 temperature=0.15, max_tokens=1800)
    # strip accidental markdown fences
    if code.startswith("```"):
        code = code.split("```", 2)[1] if code.count("```") >= 2 else code
        code = code.replace("openscad", "", 1).strip("`\n ")
    return code.strip()


# --------------------------------------------------------------------------- #
# Render OpenSCAD -> preview PNG + STL + bounding box
# --------------------------------------------------------------------------- #
def render_openscad(scad_code: str, out_stem: str, cfg: dict) -> dict:
    """Render scad_code to {out_stem}.stl + {out_stem}.png; return
    {stl_path, preview_path, dimensions_mm}. Raises RuntimeError with the
    OpenSCAD stderr on failure (so the caller can re-prompt the LLM)."""
    scad_path = out_stem + ".scad"
    stl_path = out_stem + ".stl"
    png_path = out_stem + ".png"
    with open(scad_path, "w") as f:
        f.write(scad_code)

    osc = cfg["openscad"]
    base = list(osc.get("render_cmd", ["xvfb-run", "-a", "openscad"]))
    w, h = osc.get("render_size", [800, 600])

    # STL (mesh) — no GL needed
    _run(base + ["-o", stl_path, scad_path], timeout=osc.get("timeout_s", 120))
    # PNG preview — needs GL (xvfb)
    try:
        _run(base + ["-o", png_path, f"--imgsize={w},{h}",
                     "--colorscheme=" + osc.get("colorscheme", "Tomorrow"),
                     "--viewall", "--autocenter", scad_path],
             timeout=osc.get("timeout_s", 120))
    except RuntimeError as e:
        _log(f"preview PNG render failed (non-fatal): {e}")
        png_path = ""   # design approval can still proceed with dimensions only

    bounds = stl_bounds(stl_path)
    check_printable(bounds, _default_bed(cfg))   # grounded? fits the bed? (else raises -> retry)
    return {"stl_path": stl_path, "preview_path": png_path,
            "dimensions_mm": bounds["size"], "bounds": bounds}


def _default_bed(cfg: dict) -> list:
    for p in cfg.get("printers", {}).values():
        if p.get("default"):
            return p.get("bed_mm", [220, 220, 250])
    return [220, 220, 250]


def _run(cmd: list[str], timeout: int) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "render failed").strip()[:800])
    return proc.stdout


def stl_bounds(stl_path: str) -> dict:
    """Full {min,max,size} mm bounds from a binary or ASCII STL."""
    with open(stl_path, "rb") as f:
        head = f.read(5)
        f.seek(0)
        data = f.read()
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    if head == b"solid" and b"facet" in data[:2048]:
        for line in data.decode("utf-8", "ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                xs.append(float(parts[1])); ys.append(float(parts[2])); zs.append(float(parts[3]))
    else:  # binary STL
        (n_tri,) = struct.unpack("<I", data[80:84])
        off = 84
        for _ in range(n_tri):
            vals = struct.unpack_from("<12f", data, off)  # normal(3)+v0(3)+v1(3)+v2(3)
            for vi in range(3, 12, 3):
                xs.append(vals[vi]); ys.append(vals[vi + 1]); zs.append(vals[vi + 2])
            off += 50
    if not xs:
        return {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 0, "y": 0, "z": 0},
                "size": {"x": 0, "y": 0, "z": 0}}
    mn = {"x": min(xs), "y": min(ys), "z": min(zs)}
    mx = {"x": max(xs), "y": max(ys), "z": max(zs)}
    return {"min": {k: round(v, 2) for k, v in mn.items()},
            "max": {k: round(v, 2) for k, v in mx.items()},
            "size": {k: round(mx[k] - mn[k], 2) for k in ("x", "y", "z")}}


def stl_bounding_box(stl_path: str) -> dict:
    """{x,y,z} mm extents (size only) — back-compat shim over stl_bounds()."""
    return stl_bounds(stl_path)["size"]


def check_printable(bounds: dict, bed_mm: list, tol: float = 0.5) -> None:
    """Raise if the model isn't grounded (sits below the bed) or won't fit the
    bed. 'Grounded' catches the classic LLM-CAD failure (center=true → half under
    the plate → printing into air → spaghetti)."""
    minz = bounds["min"]["z"]
    if minz < -tol:
        raise RuntimeError(
            f"model is not grounded: it extends {abs(minz):.1f}mm BELOW the build "
            f"plate (z<0). Make the part sit entirely on z>=0 with a flat base on "
            f"the bed (avoid center=true on the Z axis).")
    sx, sy, sz = bounds["size"]["x"], bounds["size"]["y"], bounds["size"]["z"]
    bx, by, bz = bed_mm
    if sx > bx + tol or sy > by + tol or sz > bz + tol:
        raise RuntimeError(
            f"model {sx}x{sy}x{sz}mm exceeds the {bx}x{by}x{bz}mm build volume.")


# --------------------------------------------------------------------------- #
# DECORATIVE path — Meshy text-to-3D
# --------------------------------------------------------------------------- #
def generate_meshy(prompt: str, out_stem: str, cfg: dict) -> dict:
    """Submit a Meshy text-to-3D preview, poll, download the STL + thumbnail.
    Returns {stl_path, preview_path, dimensions_mm}. Requires MESHY_API_KEY."""
    key = os.environ.get("MESHY_API_KEY", "")
    if not key:
        raise RuntimeError("MESHY_API_KEY not set — decorative path unavailable "
                           "(get a key at meshy.ai, add MESHY_API_KEY to the env)")
    mc = cfg["meshy"]
    base = mc["base_url"].rstrip("/")
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    task_id = _meshy_post(f"{base}/openapi/v2/text-to-3d", hdr, {
        "mode": "preview", "prompt": prompt[:600],
        "ai_model": mc.get("ai_model", "latest"),
        "target_formats": ["stl", "glb"], "should_remesh": True,
    })
    result = _meshy_poll(f"{base}/openapi/v2/text-to-3d/{task_id}", hdr,
                         mc.get("poll_timeout_s", 300))
    stl_url = (result.get("model_urls") or {}).get("stl")
    thumb = result.get("thumbnail_url") or ""
    if not stl_url:
        raise RuntimeError("Meshy returned no STL url")
    stl_path = out_stem + ".stl"
    _download(stl_url, stl_path)
    preview_path = ""
    if thumb:
        preview_path = out_stem + ".png"
        try:
            _download(thumb, preview_path)
        except (urllib.error.URLError, OSError):
            preview_path = ""
    return {"stl_path": stl_path, "preview_path": preview_path,
            "dimensions_mm": stl_bounding_box(stl_path), "meshy_task_id": task_id}


def _meshy_post(url: str, hdr: dict, body: dict) -> str:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    return out.get("result") or out.get("id") or out["task_id"]


def _meshy_poll(url: str, hdr: dict, timeout_s: int) -> dict:
    deadline = timeout_s
    waited = 0
    while waited < deadline:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
        status = out.get("status")
        if status == "SUCCEEDED":
            return out
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"Meshy task {status}: {out.get('task_error') or ''}")
        time.sleep(5)
        waited += 5
    raise RuntimeError("Meshy task timed out")


def _download(url: str, path: str) -> None:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
        f.write(resp.read())
