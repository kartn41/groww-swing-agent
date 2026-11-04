"""Intraday (MIS) equity charges. Rates are PLACEHOLDERS: verify against Groww's current price list
and NSE circulars before trusting any P&L number. Unlike delivery: STT on the sell side only,
lower stamp duty, no DP charge."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class IntradayCosts:
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.1
    brokerage_min: float = 5.0
    stt_sell_pct: float = 0.025
    exchange_txn_pct: float = 0.00297
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0


def intraday_charges(value: float, side: str, c: IntradayCosts = IntradayCosts()) -> float:
    if value <= 0:
        return 0.0
    sell = side.upper() == "SELL"
    brokerage = max(c.brokerage_min, min(c.brokerage_flat, value * c.brokerage_pct / 100))
    exch = value * c.exchange_txn_pct / 100
    sebi = value * c.sebi_pct / 100
    gst = (brokerage + exch + sebi) * c.gst_pct / 100
    stt = value * c.stt_sell_pct / 100 if sell else 0.0
    stamp = 0.0 if sell else value * c.stamp_buy_pct / 100
    return brokerage + exch + sebi + gst + stt + stamp
