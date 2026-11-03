"""Intraday session rules and risk (stricter than swing)."""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class IntradayConfig:
    bar_minutes: int = 5
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    or_end: time = time(9, 45)           # opening range = 9:15–9:45 inclusive
    entry_cutoff: time = time(14, 30)    # no new longs after this
    flat_by: time = time(15, 15)         # square off all MIS paper positions
    risk_per_trade_pct: float = 0.25
    max_positions: int = 1
    max_trades_per_day: int = 2
    daily_loss_limit_pct: float = 1.0
    min_gross_rr: float = 1.5
    max_breakout_extension: float = 0.015   # skip if close > this % above OR high


def load_intraday_config(env=None) -> IntradayConfig:
    env = os.environ if env is None else env
    return IntradayConfig(
        risk_per_trade_pct=float(env.get("INTRADAY_RISK_PCT", "0.25")),
        max_trades_per_day=int(env.get("INTRADAY_MAX_TRADES", "2")),
    )
