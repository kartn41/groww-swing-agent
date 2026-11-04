"""Intraday session rules, entry filters and risk (stricter than swing). Every field can be
overridden from .env; see ENV_OVERRIDES."""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class IntradayConfig:
    # Session. Groww labels 5m bars by their START time.
    or_end: time = time(9, 45)            # opening range = bars starting 09:15..09:40
    entry_end: time = time(11, 30)        # no new signals on bars starting after this
    flat_by: time = time(15, 15)          # square off at the open of the first bar at/after this
    min_session_bars: int = 60            # of ~75; fewer = incomplete data, symbol/day skipped

    # Entry filters
    min_or_pct: float = 0.4               # opening range width, % of price
    max_or_pct: float = 2.5
    max_extension_pct: float = 0.6        # signal close at most this far above OR high
    min_close_position: float = 0.6       # close in the top 40% of the breakout bar
    volume_mult: float = 1.5              # breakout bar volume vs session average so far
    min_rs_pct: float = 0.3               # stock minus NIFTY return since the open
    min_bar_turnover: float = 5_000_000   # average Rs traded per 5m bar so far
    require_vwap: bool = True             # signal close above session VWAP
    require_index_green: bool = True      # NIFTY above its open and its OR midpoint
    min_score: float = 50.0

    # Exits
    target_r: float = 2.0
    breakeven_at_r: float = 1.0           # move stop to entry after +1R; 0 disables
    min_risk_pct: float = 0.2             # stop distance limits, % of entry
    max_risk_pct: float = 1.2
    max_chase_pct: float = 0.3            # skip if next open is this far above the signal close

    # Risk
    risk_per_trade_pct: float = 0.25
    max_position_pct: float = 25.0        # position value cap, % of equity
    max_trades_per_day: int = 1
    stop_after_loss: bool = True
    daily_loss_limit_pct: float = 0.75


def _bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _hhmm(raw: str) -> time:
    h, m = raw.strip().split(":")
    return time(int(h), int(m))


ENV_OVERRIDES = {
    "INTRADAY_RISK_PCT": ("risk_per_trade_pct", float),
    "INTRADAY_MAX_TRADES": ("max_trades_per_day", int),
    "INTRADAY_MAX_POSITION_PCT": ("max_position_pct", float),
    "INTRADAY_DAILY_LOSS_PCT": ("daily_loss_limit_pct", float),
    "INTRADAY_TARGET_R": ("target_r", float),
    "INTRADAY_BREAKEVEN_R": ("breakeven_at_r", float),
    "INTRADAY_MIN_SCORE": ("min_score", float),
    "INTRADAY_MIN_RS_PCT": ("min_rs_pct", float),
    "INTRADAY_VOLUME_MULT": ("volume_mult", float),
    "INTRADAY_ENTRY_END": ("entry_end", _hhmm),
    "INTRADAY_INDEX_FILTER": ("require_index_green", _bool),
    "INTRADAY_VWAP_FILTER": ("require_vwap", _bool),
    "INTRADAY_STOP_AFTER_LOSS": ("stop_after_loss", _bool),
}


def load_intraday_config(env=None) -> IntradayConfig:
    env = os.environ if env is None else env
    kw = {}
    for key, (name, cast) in ENV_OVERRIDES.items():
        raw = (env.get(key) or "").strip()
        if raw:
            kw[name] = cast(raw)
    return IntradayConfig(**kw)
