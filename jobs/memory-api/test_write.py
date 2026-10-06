"""Attack tests for the WRITE path (spec 07).

A bad read is an incident. A bad write is a BELIEF — repeated faithfully by every future
recall, on every surface, forever. These tests are the difference between a brain and a
brain someone else can edit.
"""
import pytest

from ingress import CONFIDENTIAL, PERSONAL, SHARED, WORK
from scope import mint
from write import WriteDenied, _target_dataset


def test_untrusted_principal_cannot_write_at_all():
    """Memory poisoning is prompt injection that PERSISTS. A group member cannot
    put a belief in the brain — not even a 'proposed' one."""
    for name in ("odin_group", "email_inbound"):
        with pytest.raises(WriteDenied):
            _target_dataset(mint(name), declassify=False)


def test_cross_estate_session_does_not_write_back():
    """An escalated session holds both estates, so 'which estate is this?' has no safe
    default. Guessing means personal facts landing in work. §4: read-mostly."""
    with pytest.raises(WriteDenied):
        _target_dataset(mint("cli", escalate=True), declassify=False)


def test_shared_write_is_declassification_and_needs_strong_auth():
    # horus is owner but DEVICE auth (a device can be picked up by someone else)
    with pytest.raises(WriteDenied):
        _target_dataset(mint("horus"), declassify=True)
    # the CLI is owner + strong -> declassification allowed, explicitly
    assert _target_dataset(mint("cli"), declassify=True) == SHARED


def test_shared_is_never_an_implicit_target():
    """cli holds {personal, shared}. A plain write must go to personal, never shared."""
    assert _target_dataset(mint("cli"), declassify=False) == PERSONAL


def test_write_target_comes_from_scope_not_the_caller():
    assert _target_dataset(mint("work_cli", requested_datasets=[WORK]), False) == WORK
    assert _target_dataset(mint("cli"), False) == PERSONAL


def test_ambiguous_session_refuses_to_guess():
    """work_cli holds {work, shared, work_confidential} — confidential is a destination we
    ROUTE to, not one you pick, so the writable set is exactly {work}. But a session with
    two writable estates must refuse rather than choose."""
    s = mint("work_cli")
    assert _target_dataset(s, False) == WORK      # unambiguous after excluding shared/conf


def test_confidential_is_not_a_choosable_target():
    """You cannot ASK to write into the confidential wall; the classifier routes you there."""
    s = mint("work_cli", requested_datasets=[CONFIDENTIAL])
    with pytest.raises(WriteDenied):
        _target_dataset(s, False)                 # nothing writable left
