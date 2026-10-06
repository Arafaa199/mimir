"""The one shape recall returns (spec 07). Its own module so the spine and the floor can
both produce it without importing each other."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Passage:
    """One retrieved item.

    `pointer_only` items carry NO content, ever — that is the confidential tier, and it
    must be true whether the item came from the spine or from the floor.
    `origin` says which store answered, so a caller (and a human reading the audit) can
    tell a graph answer from an incumbent-store answer.
    """

    estate: str
    text: str
    origin: str = "spine"          # "spine" (cognee) | "floor" (pgvector)
    pointer_only: bool = False
    title: Optional[str] = None
    source: Optional[str] = None
