"""Provenance + scope model for the memory spine (spec 05 §4, Fable-locked 2026-07-10).

§4 makes **every** spine record carry a four-field provenance tuple from the FIRST
backfilled record: `(source, source_trust, estate, sensitivity)`. It is the day-one
invariant — it cannot be retrofitted, because the classification decisions (which wall,
what trust) are not recoverable once content is chunked into a graph.

The walls live at STORAGE: cognee ACL-ON, one physical dataset per estate. The dataset
is DERIVED from (estate, sensitivity), never chosen by a caller and never parsed from
message text:

    estate ∈ {personal, work, shared}     the base wall
    sensitivity ∈ {normal, confidential}  confidential = NDA'd / client / PII
    dataset = work_confidential   if estate == work and sensitivity == confidential
            = estate              otherwise

`work_confidential` is special (§4 confidential tier): its content is NDA'd, cognify
ships every chunk to the LLM, and it must never reach a free/logging provider. In v1 we
therefore **do not ingest its content at all** — POINTER-NOT-INGEST: the dataset holds a
stub (title + provenance + a one-line marker), never the body. That keeps confidential
material off every LLM while still making the fact of the document retrievable, and it is
exactly the spec's stated preference for "the worst documents". Full confidential ingest
waits on an approved-provider ruling.

`source_trust` is the trust of the record's ORIGIN. For live ingress it gates
scope-minting and escalation (§4 ingress registry); for backfilled history it records
where the content came from so that later tightening is possible:

    owner     the owner authored it (his own notes, corrections, docs)
    agent     an agent derived it (doc-seeder, zeroclaw observations, synthesis)
    external  ingested from outside the owner (meeting transcripts, inbound email)

No backfill source is `untrusted` — that class (Telegram group, forgeable inbound
From:) only enters live, through the ingress registry, and is propose-only with no
escalation. It is defined here so the one enum serves both paths.
"""
import os
import re
from pathlib import Path

# ---------------------------------------------------------------- enumerations
SOURCE_TRUST = ("owner", "agent", "external", "untrusted")
SENSITIVITY = ("normal", "confidential")
ESTATES = ("personal", "work", "shared")
DATASETS = ("personal", "work", "shared", "work_confidential")


def dataset_for(estate: str, sensitivity: str) -> str:
    if estate == "work" and sensitivity == "confidential":
        return "work_confidential"
    if estate not in ESTATES:
        # tie-break to the most restrictive real wall, never silently to personal
        return "work"
    return estate


def is_pointer_only(dataset: str) -> bool:
    """work_confidential is pointer-not-ingest in v1: its body never reaches an LLM."""
    return dataset == "work_confidential"


# ---------------------------------------------------------------- source_trust
# meeting/call transcripts: external participants, ingested — not owner-authored
_EXTERNAL_VAULT_PREFIXES = ("Recordings/",)
_AGENT_MEMORY_SOURCES = ("agent_observation", "synthesis", "tool_result")


def source_trust_for(provenance: str, extra: dict | None = None) -> str:
    """Classify the origin's trust for a backfilled unit.

    Deliberately conservative: an owner-authored homelab note is `owner`; an agent's
    own observation is `agent`; a meeting transcript is `external`. None of the three
    backfill stores contains untrusted-principal content (that arrives live).

    The memory source is parsed from the provenance string itself
    (`memory:{namespace}/{source}`), so no extra state has to be threaded through the
    extractor.
    """
    if provenance.startswith("vault:"):
        path = provenance.split(":", 1)[1]
        return "external" if path.startswith(_EXTERNAL_VAULT_PREFIXES) else "owner"
    if provenance.startswith("memory:"):
        tail = provenance.split(":", 1)[1]           # "{namespace}/{source}"
        src = tail.split("/", 1)[1] if "/" in tail else ""
        if src == "user_correction":
            return "owner"
        if src in _AGENT_MEMORY_SOURCES:
            return "agent"
        # `migration`/`doc_seed` rows are the owner's own documentation, agent-seeded
        return "owner" if src in ("migration", "doc_seed") else "agent"
    if provenance.startswith("brain:"):
        return "agent"
    return "agent"


# ---------------------------------------------------------------- sensitivity
# Confidential = NDA'd / client / PII material. The owner's call (2026-07-10): BROADEN to ALL
# client material — every Work client project/product/name is confidential and stays
# pointer-only, never cognified. Consequence, accepted: most of the work estate becomes
# pointer-only and loses graph recall; the residual non-client internal work is what
# cognifies. A wrong work->confidential call only over-restricts; confidential->work is
# an NDA leak, so the boundary errs toward confidential.
CONFIDENTIAL_PATH_COMPONENTS = tuple(
    c.strip().lower()
    for c in os.environ.get(
        "MIMIR_CONFIDENTIAL_DIRS",
        "HR,Hiring,Offboarding,Recruitment,Candidates,Assessments,"
        "Deposition,Legal,Contracts,"
        "Clinical,Patient,Training_Material,"
        "Payroll,"
        "Security,Credentials,Entra,Secrets"
    ).split(",")
    if c.strip()
)
# Client projects, products and named clients under NDA. ANY mention (word boundary, in a
# work-estate unit) makes it confidential -> pointer-only. The list is deployment data:
# MIMIR_CLIENT_TERMS (comma-separated) wins, else `client_terms` from the estates config
# (see estates.py: MIMIR_ESTATES_FILE / estates.json / estates.example.json).
def _client_terms() -> tuple:
    raw = os.environ.get("MIMIR_CLIENT_TERMS")
    if raw is not None:
        terms = raw.split(",")
    else:
        from estates import load_config
        terms = load_config().get("client_terms", [])
    out = tuple(t.strip().lower() for t in terms if t.strip())
    if not out:
        raise RuntimeError("no client terms configured (MIMIR_CLIENT_TERMS or "
                           "client_terms in the estates config)")
    return out


CLIENT_TERMS = _client_terms()
_CLIENT_RX = re.compile(r"\b(" + "|".join(re.escape(t) for t in CLIENT_TERMS) + r")\b",
                        re.IGNORECASE)
# PII / legal-sensitivity content markers (independent of client identity).
_CONFIDENTIAL_CONTENT_RX = re.compile(
    r"\b(national insurance|NHS number|passport no|date of birth|salary|"
    r"disciplinary|grievance|safeguarding|patient|service user|"
    r"under NDA|confidential|do not distribute)\b",
    re.IGNORECASE,
)


def sensitivity_for(provenance: str, estate: str, text: str) -> str:
    """Only work material can be confidential (personal/shared never route to the
    confidential wall). Confidential if: a confidential path dir, OR any client
    project/name, OR a PII/legal marker."""
    if estate != "work":
        return "normal"
    body = provenance.split(":", 1)[1] if ":" in provenance else provenance
    parts = {p.lower() for p in re.split(r"[/#]", body)}
    if parts & set(CONFIDENTIAL_PATH_COMPONENTS):
        return "confidential"
    head = f"{body}\n{text[:4000]}"
    if _CLIENT_RX.search(head) or _CONFIDENTIAL_CONTENT_RX.search(text[:4000]):
        return "confidential"
    return "normal"


# ---------------------------------------------------------------- record shape
def build_provenance(provenance: str, estate: str, text: str) -> dict:
    """The mandatory §4 tuple + the derived storage target, for one spine unit."""
    sensitivity = sensitivity_for(provenance, estate, text)
    dataset = dataset_for(estate, sensitivity)
    return {
        "source": provenance,
        "source_trust": source_trust_for(provenance),
        "estate": estate,
        "sensitivity": sensitivity,
        "dataset": dataset,
        "pointer_only": is_pointer_only(dataset),
    }


def pointer_stub(title: str, prov: dict) -> str:
    """The only thing a confidential unit contributes to the graph: a retrievable
    marker that the document EXISTS and where it lives — never its content."""
    return (f"# CONFIDENTIAL POINTER (content not ingested — spec 05 §4)\n"
            f"# TITLE: {title}\n"
            f"# SOURCE: {prov['source']}\n"
            f"# ESTATE: work_confidential\n"
            f"# SOURCE_TRUST: {prov['source_trust']}\n"
            f"This work_confidential document is registered but its body is NOT in the "
            f"spine. Retrieve the source directly under owner+work+strong-auth scope.")


def as_document(title: str, text: str, prov: dict) -> str:
    """Text handed to cognee. Confidential units contribute only a pointer stub; every
    other unit carries its provenance header so the graph itself records origin."""
    if prov["pointer_only"]:
        return pointer_stub(title, prov)
    return (f"# SOURCE: {prov['source']}\n"
            f"# ESTATE: {prov['estate']}\n"
            f"# SOURCE_TRUST: {prov['source_trust']}\n"
            f"# SENSITIVITY: {prov['sensitivity']}\n"
            f"# TITLE: {title}\n\n{text}")
