#!/usr/bin/env python
"""Draft a held-out parity query set for stage D — for the owner to VERIFY, not to trust.

The go/no-go rests on this set, so it is built to be hard to game:

* FAIRNESS. Questions are drawn only from documents an INCUMBENT can actually reach:
  `memory.entries` (via `memory.hybrid_search`) and `raw.notes_index` (via
  `search.embeddings` / the Synapse search API). `brain.db`-only content is excluded —
  no old contender in `parity.py` reads brain.db, so a question sourced there would
  hand cognee a free win.

* THE DISCRIMINATING CLASSES ARE QUOTA'D. cognee's whole case is relational and
  temporal recall; the bake-off's headline lived in that band. A set that drifts to
  90% factual (as the first draft did) cannot detect the thing being decided. Each
  class gets a fixed quota, and documents are PRE-SCREENED for the signal a class
  needs — dates for temporal, multiple named entities for relational, a shared entity
  across two documents for cross-source.

* NO CHERRY-PICKING. Documents are sampled mechanically (stratified by source, evenly
  spaced over a length-sorted list). Nothing is chosen by looking at how any system
  answers it. Auto-generated pages (templates, dashboards, archives) are skipped —
  they produce junk questions.

* THE LEAK PROBE CANNOT FALSE-POSITIVE. `forbidden_markers` are STRONG entities of the
  OTHER estate, minus anything appearing in the question, its ground truth, or the
  source excerpt. Strong-only matters: a personal unit may legitimately contain one
  lone WEAK term (`rota`, `payroll`) — that is exactly why weak terms need corroboration —
  and a correct personal answer quoting it must not be scored as a leak. The estate
  rule guarantees no personal unit contains a strong work entity.

* THE MODEL CANNOT SMUGGLE THE ANSWER. A drafted query is dropped if it contains its
  own ground truth, or if the ground truth's content words are not actually present in
  the source excerpt (i.e. the model invented it).

* NO CLOSED-BOOK QUESTIONS. Every candidate is put to the model with NO document. If
  it answers correctly from parametric knowledge, the query is dropped: cognee has an
  LLM in its answer path and the pgvector incumbents do not, so such a question would
  measure world knowledge rather than recall, and silently flatter cognee. ("What
  condition is a reversible sudden onset of confusion?" -> "Delirium" was in the first
  draft.)

Every surviving entry carries `_source` and `_excerpt` so each answer is checkable in
seconds. `parity.py` refuses to run until `_verified_by_owner` is set.

  ./with_env_prod.sh ./venv/bin/python gen_parity_queries.py --per-class 3
  -> .state/parity_queries_draft.json
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

from estates import WORK_STRONG

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
CORPUS = STATE / "corpus.jsonl"
OUT = STATE / "parity_queries_draft.json"

MODEL = os.environ.get("QUERYGEN_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Entities exclusive to the personal estate; the mirror of WORK_STRONG.
PERSONAL_TERMS = ("ender", "moonraker", "garmin", "worker", "edge", "obsidian",
                  "zeroclaw", "wazuh", "tailscale", "pihole", "syncthing")

# Only these sources are readable by an incumbent contender in parity.py.
REACHABLE_SOURCES = ("memory", "vault")
# Auto-generated pages make worthless questions.
SKIP_PROVENANCE = ("Templates/", "Dashboard", "_archive/", "TaskWarrior")

DATE_RX = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|\d{1,2}\s+"
                     r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
                     r"|\b(?:deadline|by\s+\d|before|after|then|next week)\b)", re.I)
PROPER_RX = re.compile(r"\b[A-Z][a-z]{2,}\b")
RELATION_RX = re.compile(r"\b(owns?|owner|responsible|reports? to|depends? on|assigned|"
                         r"lead|blocked by|escalat\w+|approv\w+)\b", re.I)
WORD_RX = re.compile(r"[A-Za-z0-9]{4,}")

CLASSES = ("factual", "relational", "temporal", "cross-source")
# candidates drawn per accepted query; the screens reject most of them
OVERSAMPLE = int(os.environ.get("QUERYGEN_OVERSAMPLE", "5"))

SINGLE_SYSTEM = """You write evaluation questions for a memory-retrieval benchmark.

Given ONE source document and a required CLASS, produce a question that:
- is answerable ONLY from facts stated in this document,
- has a short, checkable ground-truth answer (names, dates, numbers, decisions),
- reads like something the document's owner would genuinely ask months later,
- never quotes the answer inside the question itself.

CLASS definitions (obey the one you are given):
  factual    - one stated fact
  relational - who owns / depends on / is responsible for what (joins 2+ facts)
  temporal   - when, in what order, or by what deadline

Return STRICT JSON only: {"query": "...", "ground_truth": "..."}
If the document cannot support a good question of that class, return {"skip": true}."""

CROSS_SYSTEM = """You write evaluation questions for a memory-retrieval benchmark.

Given TWO source documents that share an entity, produce ONE question that CANNOT be
answered from either document alone — it must require a fact from each. Short,
checkable ground truth. Never quote the answer inside the question.

Return STRICT JSON only: {"query": "...", "ground_truth": "..."}
If the two documents do not genuinely connect, return {"skip": true}."""


def llm(system: str, user: str) -> str:
    key = os.environ["OPENROUTER_API_KEY"]
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lstrip().lower().startswith("json") else t
    try:
        return json.loads(t.strip())
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ eligibility
def eligible(d) -> bool:
    if d["source"] not in REACHABLE_SOURCES:
        return False
    if any(k in d["provenance"] for k in SKIP_PROVENANCE):
        return False
    return d["chars"] >= 700


def supports(d, cls: str) -> bool:
    t = d["text"]
    if cls == "temporal":
        return len(DATE_RX.findall(t)) >= 2
    if cls == "relational":
        return bool(RELATION_RX.search(t)) and len(set(PROPER_RX.findall(t))) >= 3
    return True


def shingles(text: str) -> set:
    return set(WORD_RX.findall(text.lower()[:1200]))


def near_dup(text: str, seen: list) -> bool:
    """Corpus sections repeat boilerplate; two near-identical docs would yield two
    near-identical questions, silently halving the set's power."""
    sh = shingles(text)
    if not sh:
        return True
    for prev in seen:
        inter = len(sh & prev)
        if inter / max(1, min(len(sh), len(prev))) > 0.6:
            return True
    return False


def sample(corpus, estate: str, cls: str, n: int, used: set, seen_sh: list):
    pool = [d for d in corpus
            if d["estate"] == estate and eligible(d) and supports(d, cls)
            and d["sha256"] not in used]
    by_source = defaultdict(list)
    for d in pool:
        by_source[d["source"]].append(d)

    # Cap EACH source at its share, then INTERLEAVE. Capping alone is not enough:
    # the caller accepts candidates in list order and stops at its quota, so simply
    # concatenating per-source lists means the first source in sort order ('memory')
    # supplies every accepted query and the vault is never reached. Both bugs produced
    # a set with zero Work-document questions.
    per = max(1, n // max(1, len(by_source)))
    per_source = {}
    for src in sorted(by_source):
        docs = sorted(by_source[src], key=lambda d: -d["chars"])
        step = max(1, len(docs) // max(1, per * 3))
        chosen = []
        for d in docs[::step]:
            if len(chosen) >= per:
                break
            if near_dup(d["text"], seen_sh):
                continue
            seen_sh.append(shingles(d["text"]))
            used.add(d["sha256"])
            chosen.append(d)
        per_source[src] = chosen

    picked = []
    for i in range(per):
        for src in sorted(per_source):
            if i < len(per_source[src]) and len(picked) < n:
                picked.append(per_source[src][i])
    return picked


# ------------------------------------------------------------------ validation
def content_words(s: str) -> set:
    return {w.lower() for w in WORD_RX.findall(s)}


def grounded(ground_truth: str, excerpt: str) -> bool:
    """Reject ground truth the model invented: most of its content words must occur
    in the source excerpt."""
    gt = content_words(ground_truth)
    if not gt:
        return False
    present = gt & content_words(excerpt)
    return len(present) / len(gt) >= 0.5


CLOSED_BOOK_SYSTEM = (
    "Answer from your own knowledge. If you do not know, reply exactly: UNKNOWN. "
    "Be brief.")


def answerable_without_retrieval(query: str, ground_truth: str) -> bool:
    """Drop questions a model can answer with NO document at all.

    "What condition is a reversible sudden onset of confusion?" -> "Delirium" is
    general medical knowledge. A retrieval benchmark cannot use it: cognee has an LLM
    in its answer path and the pgvector incumbents do not, so such a question measures
    parametric knowledge, not recall, and silently flatters cognee.
    """
    try:
        ans = llm(CLOSED_BOOK_SYSTEM, query)
    except Exception:  # noqa: BLE001 - a screen failure must not silently admit
        return True
    if "unknown" in ans.lower()[:20]:
        return False
    gt = content_words(ground_truth)
    if not gt:
        return False
    return len(gt & content_words(ans)) / len(gt) >= 0.6


def leaks_answer(query: str, ground_truth: str) -> bool:
    q, g = query.lower(), ground_truth.lower().strip()
    return len(g) > 3 and g in q


def forbidden_for(estate: str, query: str, ground_truth: str, excerpt: str = "") -> list:
    """Markers of the OTHER estate that can only appear via a leak.

    Two ways this probe could false-positive, both closed here:

    1. A system that merely ECHOES the question would trip any marker the question
       itself contains. So markers present in the question or its ground truth are
       excluded.
    2. A personal unit may legitimately contain a WEAK work term — one lone `rota` or
       `payroll` is not enough to make a section work, by design. A correct personal
       answer quoting it must not be scored as a leak. Only STRONG work entities are
       used as markers: the estate rule guarantees no personal unit contains one.
    """
    terms = WORK_STRONG if estate == "personal" else PERSONAL_TERMS
    blob = f"{query} {ground_truth} {excerpt}".lower()
    return [t for t in terms if t not in blob]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=3,
                    help="queries per (estate, class); 4 classes x 2 estates")
    args = ap.parse_args()

    corpus = [json.loads(l) for l in CORPUS.open() if l.strip()]
    print(f"corpus {len(corpus)} units; "
          f"{sum(1 for d in corpus if eligible(d))} reachable by an incumbent\n")

    out, used, seen_sh = [], set(), []
    for estate in ("personal", "work"):
        for cls in CLASSES:
            want = args.per_class
            # Oversample. Every screen (ungrounded, answer-in-question, closed-book)
            # rejects candidates, so requesting exactly `want` documents leaves a
            # permanent hole in the quota — the first run produced zero personal
            # temporal questions that way. Draw a deeper pool and accept until full.
            pool_size = want * OVERSAMPLE
            accepted = 0

            if cls == "cross-source":
                docs = sample(corpus, estate, "factual", pool_size * 2, used, seen_sh)
                for a, b in zip(docs[::2], docs[1::2]):
                    if accepted >= want:
                        break
                    ex_a, ex_b = a["text"][:2500], b["text"][:2500]
                    raw = llm(CROSS_SYSTEM,
                              f"DOC A ({a['provenance']}):\n{ex_a}\n\n"
                              f"DOC B ({b['provenance']}):\n{ex_b}")
                    obj = parse_json(raw)
                    if not obj or obj.get("skip") or not obj.get("query"):
                        continue
                    excerpt = ex_a + "\n---\n" + ex_b
                    if not accept(obj, excerpt, a["provenance"]):
                        continue
                    out.append(_entry(estate, cls, obj,
                                      f"{a['provenance']} + {b['provenance']}", excerpt,
                                      [a["sha256"], b["sha256"]]))
                    accepted += 1
                    print(f"  [{estate:<8}] {cls:<12} {obj['query'][:64]}")
                if accepted < want:
                    print(f"  ! {estate}/{cls}: only {accepted}/{want} survived screening")
                continue

            for d in sample(corpus, estate, cls, pool_size, used, seen_sh):
                if accepted >= want:
                    break
                excerpt = d["text"][:4000]
                raw = llm(SINGLE_SYSTEM,
                          f"CLASS: {cls}\nSOURCE: {d['provenance']}\n\n{excerpt}")
                obj = parse_json(raw)
                if not obj or obj.get("skip") or not obj.get("query"):
                    continue
                if not accept(obj, excerpt, d["provenance"]):
                    continue
                out.append(_entry(estate, cls, obj, d["provenance"], excerpt,
                                  [d["sha256"]]))
                accepted += 1
                print(f"  [{estate:<8}] {cls:<12} {obj['query'][:64]}")
            if accepted < want:
                print(f"  ! {estate}/{cls}: only {accepted}/{want} survived screening")

    # final dedupe on the questions themselves
    deduped, qseen = [], []
    for q in out:
        sh = shingles(q["query"])
        if any(len(sh & p) / max(1, min(len(sh), len(p))) > 0.7 for p in qseen):
            continue
        qseen.append(sh)
        q["id"] = f"{q['estate'][0].upper()}{len(deduped) + 1:02d}"
        deduped.append(q)

    STATE.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(deduped, indent=2, ensure_ascii=False))

    dist = defaultdict(int)
    for q in deduped:
        dist[(q["estate"], q["class"])] += 1
    print(f"\n{len(deduped)} queries -> {OUT}")
    print(f"{'estate':<10}" + "".join(f"{c:>14}" for c in CLASSES))
    for e in ("personal", "work"):
        print(f"{e:<10}" + "".join(f"{dist[(e, c)]:>14}" for c in CLASSES))
    print("\nEach entry has _source and _excerpt. Check the ground truth against the")
    print("excerpt, set _verified_by_owner=true, drop what is wrong, then rename to")
    print("parity_queries.json. parity.py refuses to run on unverified queries.")
    return 0


def accept(obj, excerpt: str, provenance: str) -> bool:
    """All screens, in cheapest-first order — the closed-book probe costs an LLM call."""
    if not grounded(obj["ground_truth"], excerpt):
        print(f"  drop (ground truth not in source) {provenance[:44]}")
        return False
    if leaks_answer(obj["query"], obj["ground_truth"]):
        print(f"  drop (answer inside question) {provenance[:48]}")
        return False
    if answerable_without_retrieval(obj["query"], obj["ground_truth"]):
        print(f"  drop (closed-book) {obj['query'][:56]}")
        return False
    return True


def _entry(estate, cls, obj, source, excerpt, shas):
    return {
        "id": "", "estate": estate, "class": cls,
        "query": obj["query"], "ground_truth": obj["ground_truth"],
        "forbidden_markers": forbidden_for(estate, obj["query"], obj["ground_truth"], excerpt),
        "_source": source,
        # `memory:zeroclaw/agent_observation` names 2,629 different units. Provenance
        # cannot identify a document; the content hash can.
        "_source_sha": shas,
        "_excerpt": excerpt[:700],
        "_verified": False,
    }


if __name__ == "__main__":
    sys.exit(main())
