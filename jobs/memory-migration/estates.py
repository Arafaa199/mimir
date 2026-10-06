"""Estate labelling for the memory spine (spec 05 §4).

Work must never leak into personal recall. A work->personal mislabel IS that leak;
personal->work is merely misfiled. So the tie-break is `work`.

Three signals, applied in order:

1. PROVENANCE (document level, decisive). A `Work` path component, a brain.db key
   under `Work/`, a `Recordings/` transcript, or a body that opens with a
   `[Work/...]` chunk header. Such a document is work in its entirety and is never
   split — every one of its sections is work.

2. CONTENT (section level). A document whose provenance says personal but whose body
   names work entities is split by markdown section, and each section is labelled on
   its own. Only mixed documents are split; the rest stay whole, because cognee builds
   a better graph from a whole document than from its fragments.

3. TIE-BREAK. Anything unresolved is work.

The term lists are deployment data, not code. They load at import time from the JSON
file named by MIMIR_ESTATES_FILE (default: `estates.json` next to this module, falling
back to the shipped `estates.example.json`). Keys:

  employer               the employer's name; STRONG unless ESTATE_WORK_WEAK=true
  work_vault_component   the vault folder / brain.db key prefix that holds work notes
  strong                 entities with no personal usage: one mention decides
  weak                   ambiguous terms that need a second, independent signal
  client_terms           client projects, products and names under NDA (read by
                         provenance.py; any mention makes a work unit confidential)

Three bugs this file exists to not repeat:

* `\\b` treats `-` as a word boundary, so `\\brota\\b` matched a hardware model
  name like `XR80-ROTA`, and plain substring matching matched `rota` inside
  `rotation`, `rotate` and `carrot as`. Identifiers are scrubbed before matching.

* Sections 1..N of a chunked memory row LOSE the `[Work/...]` header that section 0
  carries, so a per-section provenance test silently dropped 1 580 genuinely-work
  fragments (ops runbook rotations, ELB target groups) into personal recall. Provenance
  is therefore resolved ONCE, on the whole document, and inherited by every section.

* The employer's name also appears as a LABEL in personal infrastructure notes (an
  agent folder named after it, an Azure app named `<Employer>-Claude-MCP`, an app
  downtime impact table). Treating one passing mention as decisive costs ~2 000 units
  of homelab documentation their place in personal recall.

  OWNER'S CALL (2026-07-09): the employer name stays STRONG. One mention means work.
  Maximum leak-aversion, consistent with the "ambiguous -> work" tie-break: a homelab
  note that mentions the employer becoming unanswerable from personal recall is an
  acceptable price for never leaking work into it. `ESTATE_WORK_WEAK=true` demotes it,
  and the identifier scrub above still prevents `<Employer>-Claude-MCP` from
  triggering at all.
"""
import json
import os
import re
from pathlib import Path
from typing import Iterator, NamedTuple

HERE = Path(__file__).resolve().parent
_EXAMPLE_FILE = HERE / "estates.example.json"


def _estates_file() -> Path:
    explicit = os.environ.get("MIMIR_ESTATES_FILE")
    if explicit:
        return Path(explicit)
    local = HERE / "estates.json"
    return local if local.exists() else _EXAMPLE_FILE


def _load_terms(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise RuntimeError(f"estates config not found: {path}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"estates config is not valid JSON: {path}: {e}") from e
    for key in ("employer", "work_vault_component", "strong", "weak"):
        if key not in cfg:
            raise RuntimeError(f"estates config {path} is missing key {key!r}")
    if not cfg["strong"]:
        raise RuntimeError(f"estates config {path} has an empty 'strong' list")
    return cfg


def load_config() -> dict:
    """The parsed estates config. Shared with provenance.py (client_terms)."""
    return _load_terms(_estates_file())


_CFG = load_config()

EMPLOYER = _CFG["employer"].strip().lower()
# Entities with no personal usage: one mention decides the estate.
WORK_STRONG = tuple(t.strip().lower() for t in _CFG["strong"] if t.strip())
# Ambiguous outside work (a scheduling word also used at home, an ERP also used for a
# side project). These need a second, independent signal.
WORK_WEAK = tuple(t.strip().lower() for t in _CFG["weak"] if t.strip())
WORK_TERMS = WORK_STRONG + (EMPLOYER,) + WORK_WEAK

# The owner's call: the employer name is STRONG (one mention => work). Opt out deliberately.
WORK_IS_WEAK = os.environ.get("ESTATE_WORK_WEAK", "").lower() == "true"

_STRONG = WORK_STRONG + (() if WORK_IS_WEAK else (EMPLOYER,))
_ALL = WORK_TERMS


def _alt(terms: tuple) -> str:
    return "|".join(re.escape(t) for t in terms)


WORK_RX = re.compile(r"\b(" + _alt(_ALL) + r")\b", re.IGNORECASE)
STRONG_RX = re.compile(r"\b(" + _alt(_STRONG) + r")\b", re.IGNORECASE)
# `XR80-ROTA`, `<Employer>-Claude-MCP`: a term glued into a hyphenated alphanumeric
# identifier is a model or resource name, never an entity mention. Matched on BOTH
# sides of the hyphen — the term can lead the identifier as easily as trail it.
_TERMS_ALT = _alt(_ALL)
IDENT_RX = re.compile(
    rf"(?:[A-Za-z0-9]{{2,}}-[A-Za-z0-9-]*\b(?:{_TERMS_ALT})\b"
    rf"|\b(?:{_TERMS_ALT})\b-[A-Za-z0-9][A-Za-z0-9-]*)",
    re.IGNORECASE)
WORK_VAULT_COMPONENT = _CFG["work_vault_component"]
# A memory/brain row whose body is a chunk keyed by a work vault path.
# Was `<Component>/` — which required a literal slash right after the name, so the
# agent's `[<Component>Memory/ws-*.md :: ...]` and `[<Component>Bulk/...]` headers sailed
# straight past it. Measured 2026-07-13: 7,542 rows open with a work header; 281 were
# filed PERSONAL, 266 of them purely because of this missing `\w*`. Under dataset walls
# that is not a cosmetic mislabel — it is work content inside the personal estate.
WORK_KEY_RX = re.compile(r"^\s*\[?\s*" + re.escape(WORK_VAULT_COMPONENT) + r"\w*/",
                         re.IGNORECASE)

AMBIGUOUS_VAULT_PREFIXES = ("Recordings/",)
PERSONAL_VAULT_PREFIXES = (
    "Claude/", "Cybersecurity/", "Daily/", "Projects/", "System/",
    "Archive/", "Personal/", "Templates/",
)
PERSONAL_BRAIN_CATEGORIES = ("core", "daily", "lifeos", "infra")
PERSONAL_MEMORY_NAMESPACES = (
    "system", "lifeos", "infra", "finance", "odin", "security",
    "synthesis", "zeroclaw", "general", "global",
)

MIN_SECTION_CHARS = 200
HEADING_RX = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


class Section(NamedTuple):
    heading: str
    text: str
    estate: str
    ambiguous: bool


def _scrub(text: str) -> str:
    return IDENT_RX.sub(" ", text)


def work_terms_in(text: str) -> list[str]:
    return sorted({m.lower() for m in WORK_RX.findall(_scrub(text))})


def is_work_content(text: str) -> bool:
    """A strong entity alone, or two distinct terms of any strength."""
    clean = _scrub(text)
    if STRONG_RX.search(clean):
        return True
    return len({m.lower() for m in WORK_RX.findall(clean)}) >= 2


def is_work_keyed(text: str) -> bool:
    return bool(WORK_KEY_RX.match(text))


# ------------------------------------------------------- provenance (doc level)
def estate_for_vault(path: str, content: str = "") -> tuple[str, bool]:
    if WORK_VAULT_COMPONENT in Path(path).parts:
        return "work", False
    if path.startswith(AMBIGUOUS_VAULT_PREFIXES):
        return "work", True
    if path.startswith(PERSONAL_VAULT_PREFIXES):
        return "personal", False
    return "work", True  # unmatched -> tie-break


def estate_for_brain(key: str, category: str, content: str = "") -> tuple[str, bool]:
    if category == "work" or WORK_VAULT_COMPONENT in key.split("/"):
        return "work", False
    if is_work_keyed(content) or is_work_keyed(key):
        return "work", False
    if category in PERSONAL_BRAIN_CATEGORIES:
        return "personal", False
    return "work", True


def estate_for_memory(namespace: str, content: str = "") -> tuple[str, bool]:
    # ZeroClaw pushes Work vault chunks into memory.entries; their bodies open with
    # a `[Work.../...]` header. Decide on the WHOLE row, before any section split,
    # or sections 1..N lose the header and leak into personal.
    if is_work_keyed(content):
        return "work", False
    if namespace in PERSONAL_MEMORY_NAMESPACES:
        # A PERSONAL NAMESPACE IS NOT A PROMISE ABOUT THE CONTENT. zeroclaw/global write
        # Work material into personal namespaces, and before this guard the header
        # regex was the ONLY thing standing between that and personal recall — so the 15
        # rows whose Work-ness is in prose ("[Work circuit-breaker stuck for 36+
        # hours] ...") rather than a path header went straight through. Content wins over
        # namespace: an unmistakably-work body is work, whoever filed it.
        if is_work_content(content):
            return "work", True
        return "personal", False
    return "work", True


# -------------------------------------------------------- content (section level)
def split_markdown_sections(text: str) -> Iterator[tuple[str, str]]:
    """Yield (heading, body) per markdown heading, plus any preamble.

    Sections shorter than MIN_SECTION_CHARS fold into the previous one: a bare heading
    is not independently retrievable, and splitting on it only fragments the graph.
    """
    matches = list(HEADING_RX.finditer(text))
    if not matches:
        yield "", text
        return

    if matches[0].start() > 0:
        preamble = text[: matches[0].start()]
        if preamble.strip():
            yield "", preamble

    pending_heading, pending_body = None, ""
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(2).strip()
        body = text[m.start():end]
        if pending_heading is not None and len(pending_body) < MIN_SECTION_CHARS:
            pending_body += body
            continue
        if pending_heading is not None:
            yield pending_heading, pending_body
        pending_heading, pending_body = heading, body
    if pending_heading is not None and pending_body.strip():
        yield pending_heading, pending_body


def label_sections(text: str, doc_estate: str, doc_ambiguous: bool) -> list[Section]:
    """Split a mixed document into estate-labelled sections.

    A document already resolved to work by PROVENANCE is never split — all of it is
    work, headers and all. A personal document with no work signal is never split
    either. Only genuinely mixed documents are cut apart.
    """
    if doc_estate == "work":
        return [Section("", text, "work", doc_ambiguous)]
    if not WORK_RX.search(_scrub(text)):
        return [Section("", text, "personal", doc_ambiguous)]

    sections = []
    for heading, body in split_markdown_sections(text):
        if not body.strip():
            continue
        estate = "work" if is_work_content(heading + "\n" + body) else "personal"
        # `ambiguous` flags "no rule decided this" — a review flag, not a confidence
        # score. A section naming a client is confidently work.
        sections.append(Section(heading, body, estate, doc_ambiguous))

    if not sections:  # signal present but nothing extractable
        return [Section("", text, "work", True)]
    return sections
