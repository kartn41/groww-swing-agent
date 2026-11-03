"""Entry execution: mode isolation, kill switch, fresh-price/no-chase check, idempotency,
unknown-outcome reconciliation (never blind retry), status mapping, broker-side stop."""
from __future__ import annotations
import hashlib
import os
from dataclasses import dataclass
from datetime import date
from typing import Optional
from app.broker.base import (Broker, BrokerTimeout, BrokerUnavailable, OrderRequest, OrderStatus,
                             FILLED_STATUSES, DEAD_STATUSES, PENDING_STATUSES)
from app.config import Settings
from app.execution.state_machine import S, TradeLifecycle
from app.risk.engine import Decision, TradePlan


def make_reference_id(symbol: str, strategy: str, on: date, prefix: str = "SW") -> str:
    """Deterministic: same signal on the same day => same id => cannot double-submit.
    2-char prefix + yymmdd + 8 hex = 16 alphanumeric chars (Groww: 8-20)."""
    h = hashlib.sha1(f"{prefix}|{symbol}|{strategy}|{on.isoformat()}".encode()).hexdigest()[:8].upper()
    return f"{prefix}{on.strftime('%y%m%d')}{h}"


def kill_switch_active(settings: Settings) -> bool:
    return os.path.exists(settings.kill_switch_file)


@dataclass
class EntryResult:
    submitted: bool
    reason: str = ""
    status: Optional[OrderStatus] = None
    stop_id: Optional[str] = None
    unprotected: bool = False          # True => ALERT: holding shares with no broker-side stop


class Executor:
    def __init__(self, broker: Broker, settings: Settings) -> None:
        self.broker, self.settings = broker, settings

    def submit_entry(self, plan: TradePlan, decision: Decision, strategy: str,
                     life: TradeLifecycle, today: date, last_price: Optional[float]) -> EntryResult:
        lim = self.settings.risk

        # --- pre-trade gates (all fail closed) ---
        if kill_switch_active(self.settings):
            return self._cancel(life, "kill switch active")
        if not decision.approved or decision.qty <= 0:
            return self._cancel(life, "; ".join(decision.reasons) or "not approved")
        if last_price is None or last_price <= 0:
            return self._cancel(life, "no fresh price; cannot validate entry")
        if last_price > plan.entry * (1 + lim.max_chase_pct / 100):
            return self._cancel(life, f"price {last_price} ran beyond entry {plan.entry}; not chasing")
        if last_price <= plan.stop:
            return self._cancel(life, "price already at/below stop; setup invalidated")

        ref = make_reference_id(plan.symbol, strategy, today)
        try:
            existing = self.broker.get_order_by_reference(ref)
        except BrokerUnavailable as e:
            return self._cancel(life, f"cannot verify duplicate state ({e}); not placing")
        if existing is not None:
            return self._cancel(life, "duplicate: order already exists for this signal", existing)

        # --- place ---
        req = OrderRequest(plan.symbol, decision.qty, "BUY", "LIMIT", plan.entry, None, ref)
        life.to(S.ORDER_SUBMITTED, f"ref={ref}")
        try:
            ack = self.broker.place_order(req)
        except BrokerTimeout as e:
            ack = None
            life.note(f"outcome unknown: {e}")
        # Authoritative state always comes from a status read (acks carry no fill qty).
        try:
            st = self.broker.get_order_by_reference(ref) or ack
        except BrokerUnavailable:
            st = ack
        if st is None:
            life.to(S.RECONCILIATION_REQUIRED, "order outcome unknown; do NOT retry")
            return EntryResult(False, "outcome unknown -> reconcile before any retry")

        res = EntryResult(True, "", st)
        self._advance(life, st, decision.qty)
        if life.state in (S.OPEN, S.PARTIALLY_FILLED) and st.filled_qty > 0:
            self._protect(plan, strategy, today, st.filled_qty, res)
        return res

    # ------------------------------------------------------------------
    def _protect(self, plan, strategy, today, qty, res: EntryResult) -> None:
        sref = make_reference_id(plan.symbol, strategy, today, prefix="SL")
        try:
            res.stop_id = self.broker.place_stop_protection(plan.symbol, qty, plan.stop, sref)
        except Exception as e:
            res.unprotected = True
            res.reason = f"ALERT: filled {qty} {plan.symbol} but stop protection failed: {e}"

    @staticmethod
    def _cancel(life: TradeLifecycle, why: str, st: Optional[OrderStatus] = None) -> EntryResult:
        life.to(S.CANCELLED, why)
        return EntryResult(False, why, st)

    @staticmethod
    def _advance(life: TradeLifecycle, st: OrderStatus, want: int) -> None:
        if st.status in FILLED_STATUSES and st.filled_qty >= want:
            life.to(S.OPEN, "fully filled")
        elif st.filled_qty > 0:
            life.to(S.PARTIALLY_FILLED, f"{st.filled_qty}/{want}")
        elif st.status in DEAD_STATUSES:
            life.to(S.CANCELLED, st.status)
        elif st.status in PENDING_STATUSES:
            pass                                    # stays ORDER_SUBMITTED; never assume a fill
        else:                                       # incl. FILLED status with no fill qty
            life.to(S.RECONCILIATION_REQUIRED, f"unrecognised state {st.status}/{st.filled_qty}")


def reconcile(internal: dict[str, int], broker: Broker) -> list[str]:
    """Discrepancies between internal and broker positions. Broker outage => one explicit item,
    never an empty list (empty means 'verified in sync')."""
    try:
        b = broker.positions()
    except BrokerUnavailable as e:
        return [f"UNVERIFIED: broker positions unavailable ({e})"]
    return [f"{s}: internal={internal.get(s, 0)} broker={b.get(s, 0)}"
            for s in sorted(set(internal) | set(b)) if internal.get(s, 0) != b.get(s, 0)]
