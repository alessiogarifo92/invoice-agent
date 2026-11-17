"""The human-review state machine (phase 3, "review"): pure, no database."""

import pytest

from app.review.machine import (
    REVIEW_STATUS,
    TRANSITIONS,
    AlreadyDecided,
    InvalidTransition,
    NoteRequired,
    ProposalSuperseded,
    UnknownAction,
    decide,
)


def test_the_table_has_one_target_per_action_and_only_from_pending_approval() -> None:
    assert TRANSITIONS == {
        ("pending_approval", "approve"): "approved_for_payment",
        ("pending_approval", "clarify"): "clarification_requested",
        ("pending_approval", "block"): "blocked",
    }
    assert REVIEW_STATUS == "pending_approval"


@pytest.mark.parametrize(
    ("action", "target"),
    [
        ("approve", "approved_for_payment"),
        ("clarify", "clarification_requested"),
        ("block", "blocked"),
    ],
)
def test_following_the_proposal_needs_no_note(action: str, target: str) -> None:
    t = decide("pending_approval", action, proposed=action, note=None)
    assert (t.from_status, t.to_status, t.action, t.proposed) == (
        "pending_approval",
        target,
        action,
        action,
    )
    assert (t.override, t.lowered) == (False, False)


@pytest.mark.parametrize(
    ("proposed", "action"),
    [("block", "clarify"), ("block", "approve"), ("clarify", "approve")],
)
def test_a_less_severe_action_than_the_proposal_needs_a_note(proposed: str, action: str) -> None:
    with pytest.raises(NoteRequired) as info:
        decide("pending_approval", action, proposed=proposed, note=None)
    assert info.value.code == "note_required"
    with pytest.raises(NoteRequired):
        decide("pending_approval", action, proposed=proposed, note="   \n\t")

    t = decide("pending_approval", action, proposed=proposed, note="Called the vendor.")
    assert (t.override, t.lowered) == (True, True)


@pytest.mark.parametrize(
    ("proposed", "action"),
    [("approve", "clarify"), ("approve", "block"), ("clarify", "block")],
)
def test_a_stricter_action_is_an_override_without_a_note(proposed: str, action: str) -> None:
    t = decide("pending_approval", action, proposed=proposed, note=None)
    assert (t.override, t.lowered) == (True, False)


@pytest.mark.parametrize(
    "status",
    [
        "received",
        "processing",
        "approved_for_payment",
        "clarification_requested",
        "blocked",
        "awaiting_quota",
        "failed",
    ],
)
def test_deciding_on_an_invoice_that_is_not_pending_is_illegal(status: str) -> None:
    with pytest.raises(InvalidTransition) as info:
        decide(status, "approve", proposed="approve", note=None)
    assert info.value.code == "invalid_transition"
    assert status in str(info.value)


def test_deciding_twice_is_illegal_even_if_the_status_still_reads_pending() -> None:
    with pytest.raises(AlreadyDecided) as info:
        decide("pending_approval", "approve", proposed="approve", note=None, decided=True)
    assert info.value.code == "already_decided"


def test_a_superseded_proposal_cannot_be_decided() -> None:
    with pytest.raises(ProposalSuperseded) as info:
        decide("pending_approval", "approve", proposed="approve", note=None, current=False)
    assert info.value.code == "proposal_superseded"


@pytest.mark.parametrize("action", ["pay", "APPROVE", "", "approve "])
def test_an_unknown_action_is_refused(action: str) -> None:
    with pytest.raises(UnknownAction) as info:
        decide("pending_approval", action, proposed="approve", note=None)
    assert info.value.code == "unknown_action"


def test_an_unknown_proposed_action_fails_closed() -> None:
    with pytest.raises(ValueError, match="proposal"):
        decide("pending_approval", "approve", proposed="pay", note=None)


def test_the_unknown_action_is_reported_before_the_state() -> None:
    # A garbage action on a decided invoice says what is wrong with the request first.
    with pytest.raises(UnknownAction):
        decide("blocked", "pay", proposed="block", note=None, decided=True)
