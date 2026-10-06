"""The ingress registry — spec 07 / spec 05 §4.

Every surface that can reach memory is registered HERE, once, with the estate it belongs
to, who is speaking through it, how strongly they are authenticated, what it may see by
default, where its answers may be sent, and whether it may ever escalate.

An ingress that is not in this table gets NO SCOPE AT ALL (fail closed). That is
deliberate: a new surface must be a deliberate act of registration, not an accident of
someone calling the API with a new string.

The security claim of the whole memory layer reduces to one sentence:
**scope comes from the authenticated transport, never from the content of a message.**
This file is the transport half of that sentence; scope.py is the other half.
"""
from dataclasses import dataclass
from typing import FrozenSet

# --- the four estates (cognee datasets; ACL-ON, each its own vector+graph DB) ----------
PERSONAL = "personal"
WORK = "work"
SHARED = "shared"
CONFIDENTIAL = "work_confidential"

ALL_ESTATES = frozenset({PERSONAL, WORK, SHARED, CONFIDENTIAL})

# What an escalated (cross-estate) session may see. CONFIDENTIAL IS DELIBERATELY ABSENT.
# NDA'd client material is not the owner's to spread across a joined context, its body has
# never reached any LLM (backfill registered it pointer-only), and recall must not become
# the hole that cognify refused to be.
CROSS_ESTATE = frozenset({PERSONAL, WORK, SHARED})


@dataclass(frozen=True)
class Ingress:
    """One registered surface. Immutable: a live session can never rewrite its own rights."""

    name: str
    principal: str          # "owner" | "untrusted" | "system"
    auth: str               # "strong" | "device" | "service" | "weak" | "none"
    default_scope: FrozenSet[str]
    sink: str               # "owner_direct" | "third_party" | "propose_only"
    may_escalate: bool
    # WHICH LLM WILL SEE WHAT WE RETURN. This is a sink too — arguably the most important
    # one, and the one it is easiest to forget, because it is invisible from the request.
    #   "trusted" — a paid/no-training endpoint (Anthropic under subscription/API terms).
    #   "logging" — a free endpoint that logs prompts and may TRAIN on them.
    # Measured 2026-07-13: the Odin gateway's ONLY model in 7 days is
    # `openrouter/qwen/qwen3-coder:free`. Recall itself calls no LLM — but the CALLER does,
    # and handing it content puts that content in a third party's training corpus.
    provider: str = "trusted"
    # WHICH ESTATES THIS INGRESS MAY WRITE STAMPS TO (spec 08 §2, invariant §5.2).
    # Empty = cannot write stamps at all (every reader surface). A producer holds exactly its
    # pinned estate here and NOTHING in `default_scope` — it is write-stamps-only: it can drop
    # a recency hint into its own estate but can never READ the brain (recall/remember both
    # fail for it). A compromised producer can pollute hints, never exfiltrate memory.
    stamps_write: FrozenSet[str] = frozenset()

    @property
    def is_producer(self) -> bool:
        """A write-stamps-only surface: can stamp, cannot read. The security shape §5.2 wants."""
        return bool(self.stamps_write) and not self.default_scope

    def __post_init__(self) -> None:
        unknown = (self.default_scope | self.stamps_write) - ALL_ESTATES
        if unknown:
            raise ValueError(f"ingress {self.name}: unknown estates {sorted(unknown)}")
        if self.stamps_write and self.default_scope:
            # The load-bearing separation: a stamp WRITER must not also be a reader. If it
            # could do both, a compromised producer key would read the brain, not just pollute
            # hints. Split the roles into two ingresses if a surface genuinely needs both.
            raise ValueError(
                f"ingress {self.name}: has stamps_write AND default_scope — a producer is "
                f"write-only (invariant §5.2). Give it stamps_write and an EMPTY default_scope."
            )
        if self.stamps_write and self.may_escalate:
            raise ValueError(f"ingress {self.name}: a producer may never escalate")
        if self.may_escalate and self.principal != "owner":
            # §4: escalation is an OWNER capability. A non-owner principal that could
            # escalate would be a prompt-injection ladder into the other estate.
            raise ValueError(f"ingress {self.name}: only an owner principal may escalate")
        if self.may_escalate and self.auth not in ("strong", "device"):
            raise ValueError(f"ingress {self.name}: escalation needs strong/device auth")
        if self.provider == "logging":
            # §4, verbatim: "work/confidential context never goes to free/logging providers,
            # at cognify OR recall time." Recall closed that at cognify and made itself
            # LLM-free — and then handed the content to a caller running on a free model.
            # A config that would do that is now UNREPRESENTABLE, not merely discouraged.
            leaky = self.default_scope & {WORK, CONFIDENTIAL}
            if leaky:
                raise ValueError(
                    f"ingress {self.name}: runs on a LOGGING provider and cannot hold "
                    f"{sorted(leaky)} — that content would enter a third party's training "
                    f"corpus the moment the caller reasoned over it."
                )
            if self.may_escalate:
                raise ValueError(
                    f"ingress {self.name}: runs on a LOGGING provider and may not escalate — "
                    f"cross-estate would pull WORK into a free model's context."
                )


def _reg(*ingresses: Ingress) -> dict:
    return {i.name: i for i in ingresses}


REGISTRY = _reg(
    # --- owner-strong surfaces: the only ones that may ever cross the estate line -------
    Ingress("cli", "owner", "strong",
            frozenset({PERSONAL, SHARED}), "owner_direct", True),
    Ingress("claude_code", "owner", "strong",
            frozenset({PERSONAL, SHARED}), "owner_direct", True),
    # TRUSTED as of 2026-07-13: Odin was moved off `openrouter/qwen/qwen3-coder:free` onto the
    # owner's CLAUDE SUBSCRIPTION (an authenticated claude-shim on db-host, its own interactive
    # lane on :8089). Every model in Odin's chain is now trusted — primary Claude, fallback
    # LOCAL qwen2.5 on worker — with NO free endpoint anywhere, including the SUBAGENT chain,
    # which still pointed at qwen-free and would have silently leaked work the moment a
    # subagent recalled memory. The fallback chain IS part of the provider boundary.
    # So the work estate and cross-estate escalation are safe here again.
    # DEFAULT is personal+shared — a DM is a personal surface, and §4 says default scope
    # follows CONTEXT. Work is reachable, but only by the explicit verb (`mimir.sh recall-all`,
    # a separate command). Putting WORK in the default would make every casual question
    # cross-estate and render the verb meaningless — least privilege, and the escalation stays
    # a thing the owner DOES rather than a thing that happens to him.
    Ingress("odin_dm_owner", "owner", "strong",
            frozenset({PERSONAL, SHARED}), "owner_direct", True, provider="trusted"),
    # The work surface is the ONLY place confidential material is ever in scope, and even
    # there it yields pointers, never content (see recall.py).
    Ingress("work_cli", "owner", "strong",
            frozenset({WORK, SHARED, CONFIDENTIAL}), "owner_direct", True),
    Ingress("m365", "owner", "strong",
            frozenset({WORK, SHARED, CONFIDENTIAL}), "owner_direct", True),
    # Glasses: the owner's device, but a device can be picked up by someone else, so it is
    # device-auth, not strong-auth. It may escalate; it may not see confidential.
    # Glasses: a device can be picked up by someone else, so device-auth, not strong. And it
    # runs on Gemini Live — a Google endpoint on a free key, i.e. logging. No escalation.
    Ingress("horus", "owner", "device",
            frozenset({PERSONAL, SHARED}), "owner_direct", False, provider="logging"),

    # --- untrusted principals: NO MEMORY AT ALL. -----------------------------------------
    # These two are not just "untrusted input" — they are THIRD-PARTY SINKS. A Telegram
    # group contains other people and the assistant REPLIES INTO THE GROUP; an inbound email
    # is answered back to whoever sent it. So a memory read here does not merely risk acting
    # on injected input — it PUBLISHES the owner's memory to people who are not the owner.
    #
    # This was originally scoped to {personal}, reading §4's "untrusted ⇒ single estate,
    # propose-only" too literally. `propose_only` protects against unauthorised ACTIONS; it
    # does nothing about a leak, because the leak IS the reply. §4's real rule — "cross-estate
    # context goes to owner-direct sinks only" — generalises: PERSONAL context goes to
    # owner-direct sinks only. A group chat is not one.
    #
    # Empty scope. These surfaces can still talk to an assistant; they simply cannot read
    # the owner's brain, and (see write.py) they can never write to it either.
    Ingress("odin_group", "untrusted", "weak",
            frozenset(), "third_party", False),
    Ingress("email_inbound", "untrusted", "none",
            frozenset(), "third_party", False),

    # --- scheduled jobs: scope PINNED at registration, never negotiated at runtime -------
    # cracks-brief is the precedent §4 names: a cross-estate reader whose only sink is a
    # notify-only message to the owner. It never escalates because it never needs to ask.
    Ingress("cracks_brief", "system", "service",
            CROSS_ESTATE, "owner_direct", False),
    # fab reasons on local qwen + OpenRouter free-tier. Personal only; never work.
    Ingress("fab", "owner", "service",
            frozenset({PERSONAL, SHARED}), "owner_direct", False, provider="logging"),

    # --- hermes: the work assistant on the homelab group (consumer migration 2026-07-17)
    # Migrated off brain.db (zeroclaw-recall) onto the one API, replacing the redact_confidential
    # band-aid with native scope. WORK + SHARED only — deliberately NO work_confidential, not even
    # pointers: a group agent must not surface client-NDA existence, and this is STRICTER than the
    # brain.db path it replaces.
    #
    # ⚠️ THE EXCEPTION, MADE EXPLICIT: hermes replies INTO the homelab group ($TELEGRAM_GROUP_ID), which is
    # a third-party sink by shape. Work recall to a group sink violates "work → owner-direct sinks
    # only" — permitted SOLELY because the owner ruled the group is him-only (verified 2 members:
    # owner + bot, 2026-07-13) and hermes is his work assistant there. sink is owner_direct on
    # that basis ALONE. **If anyone is ever added to the $TELEGRAM_GROUP_ID group, REVOKE this ingress** (drop the
    # key + remove the mimir-work skill from hermes). No escalation — a group agent must NEVER
    # reach personal, ever. Trusted provider (Odin is on the Claude subscription + local qwen).
    Ingress("odin_hermes_work", "owner", "service",
            frozenset({WORK, SHARED}), "owner_direct", False, provider="trusted"),

    # --- stamp producers: WRITE-ONLY, no read, ever (spec 08 §4 + invariant §5.2) ---------
    # These push recency HINTS (entity, when, where-to-look) from the laptop. They hold a
    # pinned stamps_write estate and an EMPTY default_scope, so:
    #   * they cannot recall (empty scope -> nothing to read),
    #   * they cannot remember (principal != owner -> write.py refuses; and §5.1: third-party
    #     text never becomes a memory in v1),
    #   * they can only stamp their OWN estate (POST /stamps checks stamps_write).
    # A leaked producer key pollutes hints at worst; it can never exfiltrate the brain.
    # m365 mail is work; imessage is personal — the estate is pinned here, not chosen at write.
    Ingress("m365_producer", "system", "service",
            frozenset(), "propose_only", False, stamps_write=frozenset({WORK})),
    Ingress("imessage_producer", "system", "service",
            frozenset(), "propose_only", False, stamps_write=frozenset({PERSONAL})),
    # The owner's iOS app (2026-07-20, consumer-migration completion): the app's memory search
    # feeds its GEMINI session — a logging provider — so this surface can NEVER hold work,
    # exactly the odin_dm_owner-on-free-tier shape. Owner's device (intake-key + app auth),
    # but the provider is the sink that matters.
    Ingress("ios", "owner", "device",
            frozenset({PERSONAL, SHARED}), "propose_only", False, provider="logging"),
)


class UnknownIngress(Exception):
    """An unregistered surface asked for memory. Fail closed — it gets nothing."""


def lookup(name: str) -> Ingress:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownIngress(
            f"ingress {name!r} is not registered. Register it in ingress.py with an "
            f"explicit estate, principal, auth strength, scope and sink — an unregistered "
            f"surface gets no scope."
        ) from None
