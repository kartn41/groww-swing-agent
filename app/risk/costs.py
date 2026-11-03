from __future__ import annotations
from app.config import CostModel


def order_charges(value: float, side: str, c: CostModel) -> float:
    """Estimated total charges (Rs) for one executed delivery order of `value` Rs."""
    if value <= 0:
        return 0.0
    side = side.upper()
    brokerage = max(c.brokerage_min, min(c.brokerage_flat, value * c.brokerage_pct / 100))
    exch = value * c.exchange_txn_pct / 100
    sebi = value * c.sebi_pct / 100
    gst = (brokerage + exch + sebi) * c.gst_pct / 100
    stt = value * c.stt_pct / 100
    stamp = value * c.stamp_duty_buy_pct / 100 if side == "BUY" else 0.0
    dp = c.dp_charge_sell if side == "SELL" else 0.0
    return brokerage + exch + sebi + gst + stt + stamp + dp


def round_trip_cost(entry: float, exit_: float, qty: int, c: CostModel) -> float:
    return order_charges(entry * qty, "BUY", c) + order_charges(exit_ * qty, "SELL", c)
