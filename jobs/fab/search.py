#!/usr/bin/env python3
"""fab.search — retrieval-first model sourcing (search existing repos, print a
proven community STL instead of gambling on generated geometry).

Provider-abstracted: Thingiverse is implemented (free public API); Printables /
Thangs / Kiln-marketplaces slot in behind the same `search()`/`fetch_stl()`
shape. `search_all()` fans out over enabled providers, merges, ranks by
popularity. A downloaded STL feeds the SAME slice→propose→approve→dispatch
pipeline as generation — retrieval only replaces the front end.

Env: THINGIVERSE_TOKEN (register a free app at thingiverse.com/apps/create).
Stdlib only (urllib). Downloads individual STL files (not the "download all"
ZIP), so no archive juggling.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

TV_API = "https://api.thingiverse.com"
# Thingiverse's WAF 403s the default Python-urllib UA — send a browser-like one.
_UA = "Mozilla/5.0 (X11; Linux x86_64) mimir-fab/1.0"


def _log(m: str) -> None:
    print(f"[fab.search] {m}", flush=True)


# --------------------------------------------------------------------------- #
# Thingiverse provider
# --------------------------------------------------------------------------- #
def _tv_get(path: str, params: dict | None = None, timeout: int = 20):
    token = os.environ.get("THINGIVERSE_TOKEN", "")
    if not token:
        raise RuntimeError("THINGIVERSE_TOKEN not set — register a free app at "
                           "thingiverse.com/apps/create and add it to fab.env")
    url = TV_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def thingiverse_search(query: str, limit: int) -> list[dict]:
    """Ranked 'things' for a query. Empty list if the token is missing/unreachable."""
    try:
        data = _tv_get(f"/search/{urllib.parse.quote(query)}/",
                       {"type": "things", "sort": "relevant", "per_page": limit})
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError) as e:
        _log(f"thingiverse search failed ({e})")
        return []
    return [_tv_normalise(h) for h in (data.get("hits") or [])[:limit]]


def _tv_normalise(hit: dict) -> dict:
    return {
        "provider": "thingiverse",
        "id": str(hit.get("id", "")),
        "title": hit.get("name", ""),
        "thumbnail_url": hit.get("thumbnail") or hit.get("preview_image") or "",
        "page_url": hit.get("public_url", ""),
        "likes": hit.get("like_count", 0) or 0,
        "downloads": hit.get("download_count", 0) or 0,
        "creator": (hit.get("creator") or {}).get("name", ""),
        "license": hit.get("license", ""),
    }


def thingiverse_stl_files(thing_id: str) -> list[dict]:
    """STL files for a thing: [{file_id, name, download_url, size}]."""
    try:
        files = _tv_get(f"/things/{thing_id}/files")
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError) as e:
        _log(f"thingiverse files failed ({e})")
        return []
    out = []
    for f in files or []:
        name = f.get("name", "")
        if name.lower().endswith(".stl"):
            out.append({"file_id": str(f.get("id", "")), "name": name,
                        "download_url": f.get("download_url", ""),
                        "size": f.get("size", 0)})
    return out


def thingiverse_download(download_url: str, dest_path: str, timeout: int = 180) -> str:
    token = os.environ.get("THINGIVERSE_TOKEN", "")
    req = urllib.request.Request(download_url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as fh:
        fh.write(resp.read())
    return dest_path


# --------------------------------------------------------------------------- #
# Provider registry + fan-out
# --------------------------------------------------------------------------- #
_PROVIDERS = {
    "thingiverse": {
        "search": thingiverse_search,
        "files": thingiverse_stl_files,
        "download": thingiverse_download,
    },
    # "printables": {...},  # unofficial API — add later
    # "kiln": {...},        # MyMiniFactory/Cults3D via Kiln — add later
}


def enabled_providers(cfg: dict) -> list[str]:
    rc = cfg.get("retrieval", {}).get("providers", {})
    return [name for name, p in rc.items() if p.get("enabled") and name in _PROVIDERS]


def search_all(query: str, cfg: dict, limit: int | None = None) -> list[dict]:
    """Fan out over enabled providers, merge. Default merge = RELEVANCE (each
    provider's own relevance order, interleaved fairly) so a viral generic model
    can't dominate a specific query; set retrieval.rank_by='popularity' to instead
    rank by likes/downloads across providers."""
    limit = limit or cfg.get("retrieval", {}).get("limit", 8)
    per_provider: list[list[dict]] = []
    for name in enabled_providers(cfg):
        try:
            per_provider.append(_PROVIDERS[name]["search"](query, limit))
        except Exception as e:  # noqa: BLE001 — one provider must not sink the rest
            _log(f"provider {name} errored: {e}")
    if cfg.get("retrieval", {}).get("rank_by") == "popularity":
        merged = rank([r for lst in per_provider for r in lst])
    else:
        merged = _interleave(per_provider)   # preserve each provider's relevance order
    return merged[:limit]


def _interleave(lists: list[list[dict]]) -> list[dict]:
    """Round-robin merge preserving each list's order (single provider => its order)."""
    out: list[dict] = []
    i = 0
    while any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst):
                out.append(lst[i])
        i += 1
    return out


def rank(results: list[dict]) -> list[dict]:
    """Popularity rank: downloads weigh more than likes; stable, deterministic."""
    return sorted(results, key=lambda r: (r.get("downloads", 0) * 2 + r.get("likes", 0)),
                  reverse=True)


def fetch_stl(provider: str, thing_id: str, dest_dir: str, file_id: str | None = None) -> dict:
    """Download the chosen STL. If file_id is omitted, pick the single STL, else
    the largest (usually the main body). Returns {stl_path, file_name, choices}."""
    prov = _PROVIDERS.get(provider)
    if not prov:
        raise RuntimeError(f"unknown provider {provider}")
    files = prov["files"](thing_id)
    if not files:
        raise RuntimeError("no STL files found for that model")
    if file_id:
        chosen = next((f for f in files if f["file_id"] == str(file_id)), None)
        if not chosen:
            raise RuntimeError(f"file_id {file_id} not among this model's STLs")
    elif len(files) == 1:
        chosen = files[0]
    else:
        chosen = max(files, key=lambda f: f.get("size", 0))  # largest = main body
    dest = os.path.join(dest_dir, "model.stl")
    prov["download"](chosen["download_url"], dest)
    return {"stl_path": dest, "file_name": chosen["name"],
            "choices": [{"file_id": f["file_id"], "name": f["name"]} for f in files]}
