from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class S(str, Enum):
    CANDIDATE = "CANDIDATE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    APPROVED = "APPROVED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


ALLOWED: dict[S, set[S]] = {
    S.CANDIDATE: {S.AWAITING_CONFIRMATION, S.CANCELLED},
    S.AWAITING_CONFIRMATION: {S.APPROVED, S.CANCELLED},
    S.APPROVED: {S.ORDER_SUBMITTED, S.CANCELLED},
    S.ORDER_SUBMITTED: {S.PARTIALLY_FILLED, S.OPEN, S.CANCELLED, S.RECONCILIATION_REQUIRED},
    S.PARTIALLY_FILLED: {S.OPEN, S.CANCELLED, S.RECONCILIATION_REQUIRED},
    S.OPEN: {S.EXIT_PENDING, S.RECONCILIATION_REQUIRED},
    S.EXIT_PENDING: {S.CLOSED, S.OPEN, S.RECONCILIATION_REQUIRED},
    S.RECONCILIATION_REQUIRED: {S.OPEN, S.PARTIALLY_FILLED, S.CLOSED, S.CANCELLED},
    S.CLOSED: set(),
    S.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


@dataclass
class TradeLifecycle:
    trade_id: str
    state: S = S.CANDIDATE
    history: list[tuple[str, str, str, str]] = field(default_factory=list)  # ts, from, to, note

    def to(self, new: S, note: str = "") -> None:
        if new not in ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {new.value}")
        self.history.append((datetime.now(timezone.utc).isoformat(), self.state.value, new.value, note))
        self.state = new

    def note(self, text: str) -> None:
        """Record an event without a state change (e.g. 'outcome unknown')."""
        self.history.append((datetime.now(timezone.utc).isoformat(), self.state.value, self.state.value, text))
