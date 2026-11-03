from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class BrokerTimeout(Exception):
    """Order request outcome UNKNOWN: it may or may not have reached the exchange."""


class BrokerUnavailable(Exception):
    """A read could not be completed. Callers must treat state as UNKNOWN, never as 'none'."""


# Order statuses per Groww annexure (2026-10-20). "OPEN" appears in Groww's own doc examples.
FILLED_STATUSES = {"EXECUTED", "DELIVERY_AWAITED", "COMPLETED"}
DEAD_STATUSES = {"REJECTED", "FAILED", "CANCELLED"}
PENDING_STATUSES = {"NEW", "ACKED", "TRIGGER_PENDING", "APPROVED", "OPEN",
                    "CANCELLATION_REQUESTED", "MODIFICATION_REQUESTED"}


# GTT statuses per Groww smart-order docs (2026-10-24)
STOP_LIVE = {"ACTIVE"}
STOP_IN_PROGRESS = {"TRIGGERED"}
STOP_GONE = {"CANCELLED", "EXPIRED", "FAILED", "COMPLETED"}


@dataclass
class StopStatus:
    stop_id: str
    status: str
    symbol: str
    qty: int
    trigger: float


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    qty: int
    side: str                 # BUY / SELL
    order_type: str           # LIMIT / SL / MARKET / SL_M
    price: Optional[float]
    trigger_price: Optional[float]
    reference_id: str         # 8-20 alphanumeric, max two hyphens (Groww rule)


@dataclass
class OrderStatus:
    broker_order_id: str
    reference_id: str
    status: str
    filled_qty: int = 0
    avg_price: float = 0.0


class Broker(ABC):
    """Strategy code depends on this interface only, never on a vendor SDK."""

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderStatus:
        """Raise BrokerTimeout whenever the outcome is not definitively known."""

    @abstractmethod
    def get_order_by_reference(self, reference_id: str) -> Optional[OrderStatus]:
        """OrderStatus if found; None ONLY if definitively not found today;
        raise BrokerUnavailable if the lookup itself failed."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderStatus: ...

    @abstractmethod
    def positions(self) -> dict[str, int]:
        """Total quantity per symbol (settled holdings + today's net). Raise BrokerUnavailable on failure."""

    @abstractmethod
    def place_stop_protection(self, symbol: str, qty: int, stop_price: float,
                              reference_id: str) -> str: ...

    # ---- broker-side stop (GTT) management; implemented by brokers that support it ----
    def get_stop(self, stop_id: str) -> Optional[StopStatus]:
        """StopStatus, None if definitively unknown to the broker; raise BrokerUnavailable on failure."""
        raise NotImplementedError

    def modify_stop(self, stop_id: str, symbol: str, qty: int, trigger: float) -> StopStatus:
        raise NotImplementedError

    def cancel_stop(self, stop_id: str) -> StopStatus:
        raise NotImplementedError
