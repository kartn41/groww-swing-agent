"""Paper broker: never touches the network. A BUY limit fills when market <= limit."""
from __future__ import annotations
import itertools
from typing import Optional
from app.broker.base import Broker, BrokerTimeout, BrokerUnavailable, OrderRequest, OrderStatus, StopStatus


class PaperBroker(Broker):
    def __init__(self) -> None:
        self._orders: dict[str, OrderStatus] = {}
        self._by_ref: dict[str, str] = {}
        self._pos: dict[str, int] = {}
        self._stops: dict[str, tuple[str, int, float, str]] = {}
        self._ids = itertools.count(1)
        self.market: dict[str, float] = {}
        self.place_calls = 0
        # failure-injection hooks for tests
        self.fail_next_with_timeout = False     # accepted, but caller never hears back
        self.raise_before_accept: Optional[Exception] = None
        self.lookup_unavailable = False
        self.stop_fails = False
        self.partial_fill_qty: Optional[int] = None
        self.force_status: Optional[str] = None
        self.stop_status: dict[str, str] = {}
        self.stops_unavailable = False
        self.stop_calls: list[tuple] = []
        self.cash = 1e9

    def place_order(self, req: OrderRequest) -> OrderStatus:
        self.place_calls += 1
        if self.raise_before_accept is not None:
            e, self.raise_before_accept = self.raise_before_accept, None
            raise e
        oid = f"PAPER{next(self._ids):06d}"
        px = self.market.get(req.symbol)
        crosses = (px is not None and req.price is not None and
                   ((req.side == "BUY" and px <= req.price) or (req.side == "SELL" and px >= req.price)))
        filled = 0
        if crosses:
            filled = min(req.qty, self.partial_fill_qty) if self.partial_fill_qty else req.qty
        status = self.force_status or ("EXECUTED" if filled == req.qty and filled else "OPEN")
        st = OrderStatus(oid, req.reference_id, status, filled, px if filled else 0.0)
        self._orders[oid] = st
        self._by_ref[req.reference_id] = oid
        if filled:
            self._pos[req.symbol] = self._pos.get(req.symbol, 0) + (filled if req.side == "BUY" else -filled)
        if self.fail_next_with_timeout:
            self.fail_next_with_timeout = False
            raise BrokerTimeout("simulated timeout after acceptance")
        return OrderStatus(oid, req.reference_id, status, 0, 0.0)   # like Groww: no fill qty in ack

    def get_order_by_reference(self, reference_id: str) -> Optional[OrderStatus]:
        if self.lookup_unavailable:
            raise BrokerUnavailable("simulated status-API outage")
        oid = self._by_ref.get(reference_id)
        return self._orders.get(oid) if oid else None

    def cancel_order(self, broker_order_id: str) -> OrderStatus:
        st = self._orders[broker_order_id]
        if st.status in ("OPEN", "NEW", "ACKED"):
            st.status = "CANCELLED"
        return st

    def positions(self) -> dict[str, int]:
        return {k: v for k, v in self._pos.items() if v}

    def place_stop_protection(self, symbol: str, qty: int, stop_price: float, reference_id: str) -> str:
        if self.stop_fails:
            raise RuntimeError("simulated GTT rejection")
        for sid, (_, _, _, ref) in self._stops.items():       # idempotent on reference
            if ref == reference_id:
                return sid
        sid = f"PSTOP{next(self._ids):06d}"
        self._stops[sid] = (symbol, qty, stop_price, reference_id)
        self.stop_status[sid] = "ACTIVE"
        return sid

    def stops(self) -> dict:
        return dict(self._stops)

    def get_stop(self, stop_id: str) -> Optional[StopStatus]:
        if self.stops_unavailable:
            raise BrokerUnavailable("simulated smart-order API outage")
        if stop_id not in self._stops:
            return None
        sym, qty, trig, _ = self._stops[stop_id]
        return StopStatus(stop_id, self.stop_status[stop_id], sym, qty, trig)

    def modify_stop(self, stop_id: str, symbol: str, qty: int, trigger: float) -> StopStatus:
        self.stop_calls.append(("modify", stop_id, qty, trigger))
        _, _, _, ref = self._stops[stop_id]
        self._stops[stop_id] = (symbol, qty, trigger, ref)
        return self.get_stop(stop_id)

    def cancel_stop(self, stop_id: str) -> StopStatus:
        self.stop_calls.append(("cancel", stop_id))
        self.stop_status[stop_id] = "CANCELLED"
        return self.get_stop(stop_id)

    def available_cash(self) -> float:
        if self.lookup_unavailable:
            raise BrokerUnavailable("simulated funds-API outage")
        return self.cash

    def set_position(self, symbol: str, qty: int) -> None:      # test hook: simulate fills/sells
        self._pos[symbol] = qty
