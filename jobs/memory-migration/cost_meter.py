"""Measure what cognify actually costs, per LLM call — not per megabyte.

The phase-1 bake-off reported cognify cost per MB (7 docs / 75 KB / ~$0.05). The
stage-B pilot showed that is the wrong denominator: 125 documents totalling 0.06 MB
took 12.2 minutes. cognee's ECL makes a fixed handful of LLM calls PER DOCUMENT
(graph extraction + summarisation), so cost and wall-clock track document COUNT and
chunk count, not bytes. A corpus of 7 756 mostly-small memory rows therefore costs
far more than its 22.78 MB suggests.

Hooks litellm's success callback and accumulates real token usage + litellm's
computed response cost. Import and call `install()` before running cognify.
"""
import json
import threading
from pathlib import Path

import litellm

_lock = threading.Lock()
_totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}
_installed = False
_orig_acompletion = None


# Static prices (USD per token) so metering never makes a network call. litellm's
# completion_cost can fetch a remote pricing map over plain HTTP on a cache miss; that
# fetch timed out and the raw TimeoutError escaped cognee's retry wrapper and killed an
# unattended run. Metering must never be able to do that.
_PRICES = {
    "gemini-2.5-flash-lite": (0.10e-6, 0.40e-6),
    "gemini-2.5-flash": (0.30e-6, 2.50e-6),
}


def _price(model: str):
    for key, price in _PRICES.items():
        if key in (model or ""):
            return price
    return None


def _record(resp) -> None:
    usage = getattr(resp, "usage", None)
    cost = None
    # provider-reported cost, if OpenRouter attached one (no network)
    hidden = getattr(resp, "_hidden_params", None) or {}
    if isinstance(hidden, dict):
        cost = hidden.get("response_cost")
    if not cost and usage is not None:
        price = _price(getattr(resp, "model", ""))
        if price:
            cost = ((getattr(usage, "prompt_tokens", 0) or 0) * price[0]
                    + (getattr(usage, "completion_tokens", 0) or 0) * price[1])
    with _lock:
        _totals["calls"] += 1
        if usage is not None:
            _totals["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            _totals["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        if isinstance(cost, (int, float)):
            _totals["usd"] += float(cost)


def install() -> None:
    """Wrap `litellm.acompletion` itself.

    `litellm.success_callback` never fires for cognee: its adapters build an
    instructor client with `instructor.from_litellm(litellm.acompletion)`, which
    captures the function object. Wrapping the module attribute before cognee's
    adapter is constructed catches both that path and the direct
    `await litellm.acompletion(...)` calls.
    """
    global _installed, _orig_acompletion
    if _installed:
        return
    _orig_acompletion = litellm.acompletion

    async def _wrapped(*args, **kwargs):
        resp = await _orig_acompletion(*args, **kwargs)
        try:
            _record(resp)
        except Exception:  # noqa: BLE001 - metering must never break a run
            pass
        return resp

    litellm.acompletion = _wrapped
    _installed = True


def snapshot() -> dict:
    with _lock:
        return dict(_totals)


def report(docs: int, path: Path | None = None) -> dict:
    t = snapshot()
    per_doc_usd = t["usd"] / docs if docs else 0.0
    per_doc_calls = t["calls"] / docs if docs else 0.0
    out = {
        **t,
        "docs": docs,
        "usd_per_doc": per_doc_usd,
        "llm_calls_per_doc": per_doc_calls,
    }
    if path:
        path.write_text(json.dumps(out, indent=2))
    print(f"\n=== measured LLM cost ===")
    print(f"docs            {docs}")
    print(f"llm calls       {t['calls']}  ({per_doc_calls:.1f}/doc)")
    print(f"prompt tokens   {t['prompt_tokens']:,}")
    print(f"completion tok  {t['completion_tokens']:,}")
    print(f"cost            ${t['usd']:.4f}  (${per_doc_usd:.5f}/doc)")
    return out


def project(total_docs: int, docs_measured: int) -> None:
    t = snapshot()
    if not docs_measured or not t["calls"]:
        print("(no calls measured — cannot project)")
        return
    usd = t["usd"] / docs_measured * total_docs
    print(f"projected for {total_docs:,} docs: ~${usd:.2f}")
