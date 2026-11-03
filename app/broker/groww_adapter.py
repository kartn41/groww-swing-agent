"""Groww adapter (growwapi SDK). Facts from Groww docs read on 2026-10-20:
 - place_order(product=PRODUCT_CNC, segment=SEGMENT_CASH, order_reference_id: 8-20 alnum, <=2 hyphens)
 - place_order's response has NO filled quantity -> executor re-queries status after placing
 - get_order_list(segment, page, page_size<=25): today's orders incl. order_reference_id.
   Used for lookups because a successful full listing is a DEFINITIVE "not found".
 - Order statuses: see app.broker.base (annexure)
 - Holdings = settled shares; same-day buys appear in get_positions_for_user(segment=CASH)
 - OCO in CASH supports MIS only (not CNC) -> stop protection uses a GTT with CNC
 - Order placement needs a static IP registered with Groww (verify in your account)
UNVERIFIED until tested live with 1 share: response field names for positions/holdings,
whether holdings+positions double-count on T+1, and GTT SELL behaviour for CNC.
Refuses to construct unless mode == LIVE."""
from __future__ import annotations
from typing import Optional
from app.config import Mode, Settings
from app.broker.base import (Broker, BrokerTimeout, BrokerUnavailable, OrderRequest, OrderStatus, StopStatus)

MAX_LIST_PAGES = 40          # 40 x 25 = 1000 orders/day; beyond that => unknown


class LiveModeRequired(RuntimeError):
    pass


class GrowwBroker(Broker):
    def __init__(self, settings: Settings, access_token: str) -> None:
        if settings.mode is not Mode.LIVE:
            raise LiveModeRequired("GrowwBroker can only be built when mode == LIVE")
        from growwapi import GrowwAPI            # lazy import: tests never need the SDK
        self._g = GrowwAPI(access_token)
        g = self._g
        self._order_types = {"LIMIT": g.ORDER_TYPE_LIMIT, "MARKET": g.ORDER_TYPE_MARKET,
                             "SL": g.ORDER_TYPE_STOP_LOSS, "SL_M": g.ORDER_TYPE_STOP_LOSS_MARKET}
        self._sides = {"BUY": g.TRANSACTION_TYPE_BUY, "SELL": g.TRANSACTION_TYPE_SELL}

    def place_order(self, req: OrderRequest) -> OrderStatus:
        g = self._g
        try:
            r = g.place_order(
                trading_symbol=req.symbol, quantity=req.qty,
                validity=g.VALIDITY_DAY, exchange=g.EXCHANGE_NSE,
                segment=g.SEGMENT_CASH, product=g.PRODUCT_CNC,   # hard-wired: cash delivery only
                order_type=self._order_types[req.order_type],
                transaction_type=self._sides[req.side],
                price=req.price, trigger_price=req.trigger_price,
                order_reference_id=req.reference_id,
            )
            return OrderStatus(r["groww_order_id"], r.get("order_reference_id", req.reference_id),
                               r["order_status"])
        except KeyError:
            raise                                  # programming error (bad order_type/side): don't mask
        except Exception as e:
            # Network error, SDK error, HTTP 5xx, malformed response ... we cannot know whether
            # the exchange got it. Report UNKNOWN; the executor reconciles by reference id.
            raise BrokerTimeout(f"{type(e).__name__}: {e}") from e

    def get_order_by_reference(self, reference_id: str) -> Optional[OrderStatus]:
        g = self._g
        try:
            for page in range(MAX_LIST_PAGES):
                rows = g.get_order_list(segment=g.SEGMENT_CASH, page=page, page_size=25).get("order_list", [])
                for o in rows:
                    if o.get("order_reference_id") == reference_id:
                        return OrderStatus(o["groww_order_id"], reference_id, o["order_status"],
                                           int(o.get("filled_quantity") or 0),
                                           float(o.get("average_fill_price") or 0))
                if len(rows) < 25:
                    return None                    # listing complete: definitively not found
        except Exception as e:
            raise BrokerUnavailable(f"order lookup failed: {e}") from e
        raise BrokerUnavailable("order list exceeded page cap; state unknown")

    def cancel_order(self, broker_order_id: str) -> OrderStatus:
        g = self._g
        r = g.cancel_order(segment=g.SEGMENT_CASH, groww_order_id=broker_order_id)
        return OrderStatus(r["groww_order_id"], "", r["order_status"])

    def positions(self) -> dict[str, int]:
        g = self._g
        try:
            out: dict[str, int] = {}
            for h in g.get_holdings_for_user(timeout=5).get("holdings", []):
                out[h["trading_symbol"]] = out.get(h["trading_symbol"], 0) + int(h["quantity"])
            for p in g.get_positions_for_user(segment=g.SEGMENT_CASH).get("positions", []):
                out[p["trading_symbol"]] = out.get(p["trading_symbol"], 0) + int(p["quantity"])
            return {k: v for k, v in out.items() if v}
        except Exception as e:                     # incl. KeyError on unexpected payload shape
            raise BrokerUnavailable(f"positions unavailable: {e}") from e

    def place_stop_protection(self, symbol: str, qty: int, stop_price: float, reference_id: str) -> str:
        """GTT: when LTP falls to trigger, a SELL limit slightly below it is placed.
        NOT a guaranteed exit: a gap-down can open below the limit and leave it unfilled."""
        g = self._g
        limit = round(stop_price * 0.995, 2)
        r = g.create_smart_order(
            smart_order_type=g.SMART_ORDER_TYPE_GTT, reference_id=reference_id,
            segment=g.SEGMENT_CASH, trading_symbol=symbol, quantity=qty,
            product_type=g.PRODUCT_CNC, exchange=g.EXCHANGE_NSE, duration=g.VALIDITY_DAY,
            trigger_price=f"{stop_price:.2f}", trigger_direction=g.TRIGGER_DIRECTION_DOWN,
            order={"order_type": g.ORDER_TYPE_LIMIT, "price": f"{limit:.2f}",
                   "transaction_type": g.TRANSACTION_TYPE_SELL},
        )
        return r["smart_order_id"]

    # ---- GTT management (Groww smart-order docs, 2026-10-24) ----
    def _stop_from(self, r) -> StopStatus:
        return StopStatus(r["smart_order_id"], r["status"], r.get("trading_symbol", ""),
                          int(r.get("quantity") or 0), float(r.get("trigger_price") or 0))

    def get_stop(self, stop_id: str) -> Optional[StopStatus]:
        g = self._g
        try:
            r = g.get_smart_order(segment=g.SEGMENT_CASH, smart_order_type=g.SMART_ORDER_TYPE_GTT,
                                  smart_order_id=stop_id)
        except Exception as e:
            raise BrokerUnavailable(f"GTT lookup failed: {e}") from e
        return self._stop_from(r) if r else None

    def modify_stop(self, stop_id: str, symbol: str, qty: int, trigger: float) -> StopStatus:
        g = self._g
        limit = round(trigger * 0.995, 2)
        r = g.modify_smart_order(
            smart_order_id=stop_id, smart_order_type=g.SMART_ORDER_TYPE_GTT, segment=g.SEGMENT_CASH,
            quantity=qty, trigger_price=f"{trigger:.2f}", trigger_direction=g.TRIGGER_DIRECTION_DOWN,
            order={"order_type": g.ORDER_TYPE_LIMIT, "price": f"{limit:.2f}",
                   "transaction_type": g.TRANSACTION_TYPE_SELL})
        return StopStatus(r.get("smart_order_id", stop_id), r.get("status", "ACTIVE"), symbol,
                          int(r.get("quantity") or qty), trigger)

    def cancel_stop(self, stop_id: str) -> StopStatus:
        g = self._g
        r = g.cancel_smart_order(segment=g.SEGMENT_CASH, smart_order_type=g.SMART_ORDER_TYPE_GTT,
                                 smart_order_id=stop_id)
        return StopStatus(r.get("smart_order_id", stop_id), r.get("status", "CANCELLED"), "", 0, 0.0)
