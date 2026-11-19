"""Case workflow state machine.

            escalate              clear
    open ─────────────► escalated ─────► cleared          (terminal)
      │                     │
      │ clear               │ confirm_match
      ├────────► cleared    └──────────► confirmed_match  (terminal)
      │
      │ confirm_match
      └────────► confirmed_match

A first-line analyst can clear an obvious false positive or confirm a true
match directly; anything uncertain is escalated to a senior reviewer, who must
clear or confirm. Terminal states never change - a wrong decision is fixed by
opening a new screening, which keeps the audit trail honest.
"""

from __future__ import annotations

from typing import Literal

Status = Literal["open", "escalated", "cleared", "confirmed_match"]
Decision = Literal["clear", "escalate", "confirm_match"]

TRANSITIONS: dict[tuple[str, str], str] = {
    ("open", "clear"): "cleared",
    ("open", "escalate"): "escalated",
    ("open", "confirm_match"): "confirmed_match",
    ("escalated", "clear"): "cleared",
    ("escalated", "confirm_match"): "confirmed_match",
}

TERMINAL: frozenset[str] = frozenset({"cleared", "confirmed_match"})


class InvalidTransition(Exception):
    def __init__(self, status: str, decision: str):
        self.status = status
        self.decision = decision
        super().__init__(f"cannot '{decision}' a case that is '{status}'")


def next_status(status: str, decision: str) -> str:
    try:
        return TRANSITIONS[(status, decision)]
    except KeyError:
        raise InvalidTransition(status, decision) from None


def allowed_decisions(status: str) -> list[str]:
    return sorted(d for (s, d) in TRANSITIONS if s == status)
