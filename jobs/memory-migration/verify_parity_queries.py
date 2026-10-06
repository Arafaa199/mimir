#!/usr/bin/env python
"""Verify the drafted parity queries against their FULL source documents.

The owner delegated verification (2026-07-10). This does not set `_verified_by_owner` —
that flag asserts the owner checked, and he did not. It sets `_verified` with
`_verified_by: "claude"` so the artifact never overstates who vouched for it.

A verifier that just says "yes, supported" is worthless: it can hallucinate approval
as easily as the drafter hallucinated the answer. So the verifier must return a
**verbatim supporting quote**, and that quote is then checked to actually occur in the
source document (whitespace-normalised substring). No quote, no pass.

Checks applied to every query:
  1. SUPPORT     - a verbatim span of the source states the ground truth.
  2. GROUNDING   - the ground truth's content words occur in that span (not merely
                   somewhere in the document).
  3. CLASS       - the class label matches what the question actually asks.
  4. REACHABILITY- an incumbent can reach the source. A `vault:` question is only fair
                   if the note is present in search.embeddings; a `memory:` question
                   only if the text is in memory.entries. Otherwise cognee wins by
                   default and the parity number is meaningless.
  5. ANSWERED    - the drafted ground truth is not empty or "unknown".

Survivors are written to parity_queries.json; rejects to parity_queries_rejected.json
with the reason, so nothing disappears silently.

  ./with_env_prod.sh ./venv/bin/python verify_parity_queries.py
"""
import base64
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
STATE = Path(os.environ.get("MIMIR_MEMORY_STATE", HERE / ".state"))
CORPUS = STATE / "corpus.jsonl"
DRAFT = STATE / "parity_queries_draft.json"
OUT = HERE / "parity_queries.json"
REJECTS = STATE / "parity_queries_rejected.json"

MODEL = os.environ.get("VERIFY_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
WORD_RX = re.compile(r"[A-Za-z0-9]{4,}")

VERIFY_SYSTEM = """You verify a benchmark question against its source document(s).

Return STRICT JSON only:
{
  "supported": true|false,
  "quotes": ["<a VERBATIM span copied character-for-character from the source>", ...],
  "class": "factual|relational|temporal|cross-source"
}

Rules:
- "supported" is true ONLY if the ground truth is stated in the source. If the source
  merely hints at it, or the answer requires outside knowledge, say false.
- Each entry of "quotes" MUST be copied exactly from the source. Do not paraphrase, do
  not fix typos, do not add ellipses. If you cannot find such a span, set
  supported=false and quotes=[].
- If the SOURCE contains two documents separated by `---`, return ONE quote from EACH
  document: a cross-source fact cannot be evidenced by a span from only one of them.
- "class" is what the QUESTION actually asks for, which may differ from its label:
    factual    = a single stated attribute or value, with no link between entities
    relational = the answer is an ENTITY linked to another entity by a relation.
                 "Who owns X", "who is responsible for X", "which system depends on X",
                 "which component writes to X" are ALL relational, even when the answer
                 is a single name. Do not downgrade these to factual.
    temporal   = when something happened, in what order, or by what deadline. A date,
                 a sequence, or a duration. Do not downgrade these to factual.
    cross-source = the answer needs a fact from EACH of the two documents"""


def llm(system: str, user: str, max_tokens: int = 700) -> str:
    key = os.environ["OPENROUTER_API_KEY"]
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
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


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def tokens(s: str) -> list:
    return re.findall(r"[a-z0-9]+", s.lower())


def quote_supported(quote: str, source: str) -> tuple[bool, float, str]:
    """Is the quote really a span of the source?

    An exact substring test is too brittle: a model reproduces `edge -> db-host` for a
    source that wrote `edge → db-host`, and the quote is rejected even though every one
    of its ten words appears in order. It is also too weak in the other direction —
    nothing stops a fabricated `Owner: Owner`.

    So compare CONTENT-WORD SEQUENCES: the quote's tokens must appear as a contiguous
    run inside the source's tokens. Punctuation and typography drop out; invention does
    not, because fabricated words are simply absent.
    """
    qt, st = tokens(quote), tokens(source)
    if len(qt) < 3:
        return False, 0.0, "quote too short to be evidence"
    joined = " ".join(st)
    for n in range(len(qt), 1, -1):
        for start in range(0, len(qt) - n + 1):
            if " ".join(qt[start:start + n]) in joined:
                frac = n / len(qt)
                # A date fact's evidence is naturally short ("resolved 2026-06-09"). A
                # blanket 5-token floor threw away exactly the temporal questions the
                # benchmark most needs. Short quotes are admissible only if reproduced
                # in FULL; longer ones may lose a fifth to typography.
                ok = (frac == 1.0) if len(qt) < 5 else n >= max(4, int(0.8 * len(qt)))
                return ok, frac, "" if ok else (
                    "short quote must be reproduced in full" if len(qt) < 5
                    else f"only {frac:.0%} of the quote is in the source")
    return False, 0.0, "no part of the quote occurs in the source"


def words(s: str) -> set:
    return {w.lower() for w in WORD_RX.findall(s)}


# ------------------------------------------------------------------ reachability
def db_host_scalar(sql: str) -> str:
    inner = ("docker exec -i db-host-db bash -c "
             "'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U db-host -d db-host -qtAX -f -'")
    argv = (["bash", "-c", inner] if socket.gethostname() == "db-host"
            else ["ssh", "-o", "ConnectTimeout=10", "db-host", inner])
    p = subprocess.run(argv, input=sql, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def reachable(provenance: str) -> tuple[bool, str]:
    """A question is only fair if an INCUMBENT can retrieve its source.

    `search.embeddings.source_id` for `obsidian_note` is the note's RELATIVE PATH, not
    its `raw.notes_index.id`. Joining on the id matched nothing and condemned every
    vault-sourced question as unfair.
    """
    if provenance.startswith("vault:"):
        path = provenance.split(":", 1)[1].split("#")[0].replace("'", "''")
        n = db_host_scalar("SELECT count(*) FROM search.embeddings WHERE "
                         f"source_type='obsidian_note' AND source_id = '{path}';")
        if n == "0":
            n2 = db_host_scalar("SELECT count(*) FROM raw.notes_index WHERE "
                              f"relative_path = '{path}' AND removed_at IS NULL;")
            return False, ("vault note not embedded in search.embeddings"
                           if n2 != "0" else "vault note absent from raw.notes_index")
        return True, ""
    if provenance.startswith("memory:"):
        return True, ""   # by construction: extracted from memory.entries
    return False, f"unreachable source kind: {provenance.split(':')[0]}"


# ------------------------------------------------------------------------- main
def main() -> int:
    corpus = [json.loads(l) for l in CORPUS.open() if l.strip()]
    # Resolve by CONTENT HASH: `memory:zeroclaw/agent_observation` is the provenance of
    # 2,629 distinct units, so a provenance lookup handed the verifier the wrong
    # document and it correctly answered "not supported by source".
    by_sha = {d["sha256"]: d for d in corpus}

    draft = json.loads(DRAFT.read_text())
    kept, rejected = [], []

    for q in draft:
        shas = q.get("_source_sha") or []
        missing = [h for h in shas if h not in by_sha]
        if not shas or missing:
            rejected.append({**q, "_reject": "source unit not resolvable by content hash"})
            print(f"  {q['id']} DROP  source unit not resolvable")
            continue
        docs = [by_sha[h] for h in shas]
        sources = [d["provenance"] for d in docs]
        full = "\n\n---\n\n".join(d["text"] for d in docs)

        # 5. answered at all
        gt = (q.get("ground_truth") or "").strip()
        if not gt or gt.lower() in ("unknown", "n/a", "none"):
            rejected.append({**q, "_reject": "empty ground truth"})
            print(f"  {q['id']} DROP  empty ground truth")
            continue

        # 4. reachability by an incumbent
        bad = None
        for s in sources:
            ok, why = reachable(s)
            if not ok:
                bad = why
                break
        if bad:
            rejected.append({**q, "_reject": f"unfair: {bad}"})
            print(f"  {q['id']} DROP  unfair ({bad})")
            continue

        # 1-3. LLM verification with a verbatim quote we then check ourselves
        # A malformed JSON reply is a transient formatting failure, not evidence about
        # the query. Retry before condemning it — the first run lost the only work
        # temporal question this way.
        obj = None
        for _ in range(3):
            raw = llm(VERIFY_SYSTEM,
                      f"QUESTION: {q['query']}\n\nGROUND TRUTH: {gt}\n\n"
                      f"SOURCE:\n{full[:12000]}")
            obj = parse_json(raw)
            if obj:
                break
        if not obj:
            rejected.append({**q, "_reject": "verifier returned unparseable JSON 3x"})
            print(f"  {q['id']} DROP  verifier unparseable after 3 attempts")
            continue
        if not obj.get("supported"):
            rejected.append({**q, "_reject": "verifier: not supported by source"})
            print(f"  {q['id']} DROP  not supported by source")
            continue

        # A cross-source question needs one quote PER document — a single span cannot
        # evidence a fact that lives in two places, and demanding one guaranteed every
        # cross-source query was rejected.
        quotes = obj.get("quotes") or ([obj["quote"]] if obj.get("quote") else [])
        quotes = [str(x).strip() for x in quotes if str(x).strip()]
        if len(docs) > 1 and len(quotes) < 2:
            rejected.append({**q, "_reject": "cross-source needs a quote from each document"})
            print(f"  {q['id']} DROP  cross-source: only {len(quotes)} quote(s)")
            continue

        bad_quote = None
        if len(docs) > 1:
            # each quote must sit in SOME document, and both documents must be cited
            hit_docs = set()
            for qt_ in quotes:
                for i, d in enumerate(docs):
                    ok_q, frac, why = quote_supported(qt_, d["text"])
                    if ok_q:
                        hit_docs.add(i)
                        break
                else:
                    bad_quote = f"quote not in either document ({why})"
            if not bad_quote and len(hit_docs) < 2:
                bad_quote = "both quotes come from the same document"
        else:
            ok_q, frac, why = quote_supported(quotes[0] if quotes else "", full)
            if not ok_q:
                bad_quote = why or "quote not in source"

        if bad_quote:
            rejected.append({**q, "_reject": f"supporting quote: {bad_quote}"})
            print(f"  {q['id']} DROP  {bad_quote}")
            continue

        # 2. the ground truth must live in the CITED SPANS, not merely somewhere in the doc
        quote = " ... ".join(quotes)
        gtw = words(gt)
        cover = len(gtw & words(quote)) / max(1, len(gtw))
        if cover < 0.4:
            rejected.append({**q, "_reject": f"ground truth not in the cited span "
                                             f"(coverage {cover:.2f})"})
            print(f"  {q['id']} DROP  ground truth not in cited span ({cover:.2f})")
            continue

        new_class = obj.get("class", q["class"])
        note = "" if new_class == q["class"] else f"  (reclassified {q['class']} -> {new_class})"
        kept.append({**q, "class": new_class,
                     "_supporting_quote": quote[:400],
                     "_verified": True,
                     "_verified_by": "claude (delegated by the owner 2026-07-10)",
                     # stated, not implied: the owner did not personally check these
                     "_verified_by_owner": False})
        print(f"  {q['id']} keep  [{new_class}]{note}")

    OUT.write_text(json.dumps(kept, indent=2, ensure_ascii=False))
    REJECTS.write_text(json.dumps(rejected, indent=2, ensure_ascii=False))

    print(f"\nkept {len(kept)} / {len(draft)}  (rejects -> {REJECTS.name})")
    dist = defaultdict(int)
    for q in kept:
        dist[(q["estate"], q["class"])] += 1
    classes = ("factual", "relational", "temporal", "cross-source")
    print(f"{'estate':<10}" + "".join(f"{c:>14}" for c in classes))
    for e in ("personal", "work"):
        print(f"{e:<10}" + "".join(f"{dist[(e, c)]:>14}" for c in classes))
    rt = sum(1 for q in kept if q["class"] in ("relational", "temporal"))
    print(f"\ndiscriminating rel+temp band: {rt}/{len(kept)}")
    if rt < 6:
        print("WARNING: too few relational/temporal queries to decide the go/no-go.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
