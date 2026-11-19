import itertools

import pytest

from app.cases import TERMINAL, TRANSITIONS, InvalidTransition, allowed_decisions, next_status

STATUSES = ["open", "escalated", "cleared", "confirmed_match"]
DECISIONS = ["clear", "escalate", "confirm_match"]


@pytest.mark.parametrize(
    ("status", "decision", "expected"),
    [
        ("open", "clear", "cleared"),
        ("open", "escalate", "escalated"),
        ("open", "confirm_match", "confirmed_match"),
        ("escalated", "clear", "cleared"),
        ("escalated", "confirm_match", "confirmed_match"),
    ],
)
def test_valid_transitions(status, decision, expected):
    assert next_status(status, decision) == expected


@pytest.mark.parametrize(("status", "decision"), [
    (s, d) for s, d in itertools.product(STATUSES, DECISIONS) if (s, d) not in TRANSITIONS
])
def test_every_other_transition_is_rejected(status, decision):
    with pytest.raises(InvalidTransition):
        next_status(status, decision)


def test_terminal_states_have_no_exits():
    for status in TERMINAL:
        assert allowed_decisions(status) == []


def test_cannot_escalate_twice():
    assert "escalate" not in allowed_decisions("escalated")


def test_unknown_status_rejected():
    with pytest.raises(InvalidTransition):
        next_status("deleted", "clear")
