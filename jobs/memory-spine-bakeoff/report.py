#!/usr/bin/env python
"""Aggregate scores by query class + apply the pre-committed decision rule.
Mean relevance per class (0-2 and % of max). Discriminating band = relational
+ temporal. Emits report.md."""
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
CLASSES = ["factual", "relational", "temporal", "cross-source"]
DISCRIM = ("relational", "temporal")


def load():
    return json.loads((HERE / "results_scored.json").read_text())


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def by_class(scored, field):
    d = defaultdict(list)
    for r in scored:
        d[r["class"]].append(r[field])
    return d


def table(scored):
    # cognee "best" per query = max(chunks, graph)
    for r in scored:
        r["score_B_best"] = max(r["score_B_chunks"], r["score_B_graph"])
    cols = [("A (pgvector-hybrid)", "score_A"),
            ("B cognee-CHUNKS", "score_B_chunks"),
            ("B cognee-GRAPH", "score_B_graph"),
            ("B cognee-best", "score_B_best")]
    lines = []
    header = f"| {'class':<13} | n | " + " | ".join(f"{name}" for name, _ in cols) + " |"
    sep = "|" + "---|" * (len(cols) + 2)
    lines.append(header)
    lines.append(sep)
    agg = {}
    for c in CLASSES:
        row = f"| {c:<13} | {len(by_class(scored,'score_A')[c])} |"
        for name, f in cols:
            m = mean(by_class(scored, f)[c])
            row += f" {m:.2f} ({m/2*100:.0f}%) |"
            agg[(c, f)] = m
        lines.append(row)
    # overall + discriminating band
    for label, subset in [("ALL", CLASSES), ("DISCRIM (rel+temp)", DISCRIM)]:
        row = f"| **{label}** | {sum(len(by_class(scored,'score_A')[c]) for c in subset)} |"
        for name, f in cols:
            vals = [r[f] for r in scored if r["class"] in subset]
            m = mean(vals)
            row += f" **{m:.2f} ({m/2*100:.0f}%)** |"
            agg[(label, f)] = m
        lines.append(row)
    return "\n".join(lines), agg


def decision(agg):
    a = agg[("DISCRIM (rel+temp)", "score_A")]
    b_best = agg[("DISCRIM (rel+temp)", "score_B_best")]
    b_chunks = agg[("DISCRIM (rel+temp)", "score_B_chunks")]
    # relative margin on the discriminating band
    margin = (b_best - a) / a * 100 if a > 0 else (100.0 if b_best > 0 else 0.0)
    verdict = ("ADOPT cognee" if margin >= 30 else
               "KEEP pgvector-hybrid (incumbent wins ties / no dependency for a tie)")
    return {
        "A_discrim": round(a, 3), "B_best_discrim": round(b_best, 3),
        "B_chunks_discrim": round(b_chunks, 3),
        "relative_margin_pct": round(margin, 1), "threshold_pct": 30,
        "verdict": verdict,
    }


def main():
    scored = load()
    tbl, agg = table(scored)
    dec = decision(agg)
    out = ["# Bake-off scores\n", tbl, "\n## Decision rule (pre-committed)\n",
           f"- Adopt cognee ONLY if it beats pgvector-hybrid by ≥30% on the RELATIONAL+TEMPORAL "
           f"band AND cognify cost/maintenance is acceptable on the free/local path.",
           f"- Discriminating-band mean: A={dec['A_discrim']:.2f} · "
           f"cognee-best={dec['B_best_discrim']:.2f} · cognee-CHUNKS={dec['B_chunks_discrim']:.2f} (0-2)",
           f"- Relative margin (cognee-best vs A) = **{dec['relative_margin_pct']:.0f}%** "
           f"(threshold {dec['threshold_pct']}%)",
           f"- **Verdict: {dec['verdict']}**\n"]
    (HERE / "report.md").write_text("\n".join(out))
    print("\n".join(out))
    print("\n[report] -> report.md")


if __name__ == "__main__":
    main()
