"""Scope minting — the security kernel of the memory layer (spec 07 / spec 05 §4).

ONE RULE:

    Scope is minted from the AUTHENTICATED TRANSPORT, below the model.
    It is never parsed from message text. It is never chosen by an LLM.

Everything else here is the mechanical consequence of that rule:

  * `mint()` takes a session (ingress + principal), not a message.
  * A caller may NARROW its scope (ask for less). `_narrow()` INTERSECTS — there is no
    code path that adds an estate a session did not already hold. Widening is not
    "checked and rejected", it is *unrepresentable*.
  * Escalation to cross-estate needs an owner-strong ingress AND a verb parsed by
    `parse_escalation_verb()` — deterministic router code, not a model's judgment. A model
    that could talk itself into another estate is a model that an injected message can
    talk into another estate.
  * Scope is a frozen dataclass. A live session cannot rewrite its own rights.
"""
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional

from ingress import CONFIDENTIAL, CROSS_ESTATE, Ingress, lookup


class ScopeDenied(Exception):
    """A session asked for access it does not have. Always audited, never silently downgraded."""


@dataclass(frozen=True)
class Scope:
    """An immutable per-session capability. Carry it; never rebuild it from user input."""

    datasets: FrozenSet[str]
    ingress: str
    principal: str
    sink: str
    escalated: bool = False

    @property
    def is_cross_estate(self) -> bool:
        return len({d for d in self.datasets if d in ("personal", "work")}) > 1

    def __str__(self) -> str:
        tag = " ESCALATED" if self.escalated else ""
        return f"<{self.ingress}/{self.principal} {sorted(self.datasets)}{tag}>"


# The escalation verb. Deterministic, explicit, owner-typed. Parsed by ROUTER CODE.
# Not a synonym list, not fuzzy matching, not an intent classifier — those are all
# surfaces an injected message could imitate. The owner types the verb or gets no
# cross-estate answer.
_ESCALATION_VERBS = ("/bothestates", "/crossestate", "/wholelife")


def parse_escalation_verb(text: str) -> bool:
    """True iff the message OPENS with an explicit escalation verb.

    Call this from the ROUTER (Odin, the CLI), never from a model. It exists so that
    "cross-estate" is a thing the owner *does*, not a thing an assistant *decides* — and
    so that a message body arriving from a group chat can never trigger it (that ingress
    cannot escalate at all; see mint()).
    """
    if not text:
        return False
    first = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    return first in _ESCALATION_VERBS


def strip_escalation_verb(text: str) -> str:
    """Remove the verb before the text ever reaches a model or an embedder."""
    if not parse_escalation_verb(text):
        return text
    parts = text.strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _narrow(held: FrozenSet[str], requested: Optional[Iterable[str]]) -> FrozenSet[str]:
    """Intersect. This is the whole of "content may narrow, never widen".

    A request for an estate the session does not hold is not an error to be handled --
    it simply is not in the intersection. There is no branch that could grant it.
    """
    if not requested:
        return held
    asked = frozenset(requested)
    got = held & asked
    if not got and held:
        raise ScopeDenied(
            f"requested {sorted(asked)} but this session holds {sorted(held)} — "
            f"nothing in common. Scope can be narrowed, never widened."
        )
    return got


def mint(
    ingress_name: str,
    *,
    escalate: bool = False,
    requested_datasets: Optional[Iterable[str]] = None,
) -> Scope:
    """Mint the capability for one session. Call ONCE, at session creation.

    `escalate` must come from ROUTER CODE that ran `parse_escalation_verb()` on an
    owner-typed message — never from a model, and never from the body of a message that
    arrived on an untrusted ingress (those ingresses have may_escalate=False, so this
    argument is refused there no matter who passes it).
    """
    ing: Ingress = lookup(ingress_name)          # unregistered -> UnknownIngress, fail closed
    held = ing.default_scope

    if escalate:
        if not ing.may_escalate:
            # The load-bearing refusal. Say WHY -- there are two very different reasons, and a
            # misleading error costs someone an hour later.
            if ing.provider == "logging":
                raise ScopeDenied(
                    f"ingress {ing.name!r} runs on a LOGGING provider (a free endpoint that "
                    f"logs prompts and may train on them). Cross-estate would pull WORK into "
                    f"its context, and §4 forbids work/confidential ever reaching such a "
                    f"provider. Move this surface to a paid/local model and escalation returns."
                )
            raise ScopeDenied(
                f"ingress {ing.name!r} (principal={ing.principal}, auth={ing.auth}) may "
                f"never escalate. Cross-estate recall is an owner capability on a strong "
                f"surface; untrusted input cannot buy it."
            )
        # Escalation grants the cross-estate union -- and CONFIDENTIAL IS NOT IN IT, even
        # from the work surface that normally holds it. Joining NDA'd client material into
        # a cross-estate context is exactly what §4 forbids.
        held = (held | CROSS_ESTATE) - {CONFIDENTIAL}

    return Scope(
        datasets=_narrow(held, requested_datasets),
        ingress=ing.name,
        principal=ing.principal,
        sink=ing.sink,
        escalated=bool(escalate),
    )
