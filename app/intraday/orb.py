"""Opening-range breakout (long-only), evaluated on completed 5m bars."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time
import pandas as pd

from app.strategies.common import Rejection, Setup

NAME = "INTRADAY_ORB"


@dataclass
class OpeningRange:
    high: float
    low: float
    ready: bool


def opening_range(bars: pd.DataFrame, or_end: time) -> OpeningRange:
    """OR = all bars with time <= or_end on the session day."""
    if bars.empty:
        return OpeningRange(0.0, 0.0, False)
    day = bars.index[-1].date()
    mask = (bars.index.date == day) & (bars.index.time <= or_end)
    seg = bars.loc[mask]
    if seg.empty:
        return OpeningRange(0.0, 0.0, False)
    return OpeningRange(float(seg["high"].max()), float(seg["low"].min()), True)


def relative_strength(stock: pd.DataFrame, index: pd.DataFrame, n_bars: int = 12) -> float:
    """Session RS: stock return minus index return over the last n completed bars."""
    if len(stock) < 2 or index.empty:
        return 0.0
    seg = stock.iloc[-min(n_bars, len(stock)) :]
    idx = index["close"].reindex(seg.index).ffill().bfill()
    if idx.isna().any():
        return 0.0
    s_ret = float(seg["close"].iloc[-1] / seg["close"].iloc[0] - 1)
    i_ret = float(idx.iloc[-1] / idx.iloc[0] - 1)
    return s_ret - i_ret


def evaluate_orb(
    symbol: str,
    sector: str,
    bars: pd.DataFrame,
    index_bars: pd.DataFrame,
    or_range: OpeningRange,
    *,
    min_gross_rr: float = 1.5,
    min_rs: float = 0.001,
    max_extension: float = 0.015,
) -> Setup | Rejection:
    if not or_range.ready:
        return Rejection(symbol, "opening range not ready", NAME)
    if len(bars) < 3:
        return Rejection(symbol, "insufficient intraday bars", NAME)

    last = bars.iloc[-1]
    close, high, low, vol = float(last["close"]), float(last["high"]), float(last["low"]), float(last["volume"])
    if close <= or_range.high:
        return Rejection(symbol, f"no breakout: close {close:.2f} <= OR high {or_range.high:.2f}", NAME)
    ext = close / or_range.high - 1
    if ext > max_extension:
        return Rejection(symbol, f"extended {ext:.1%} above OR high: not chasing", NAME)

    or_seg = bars.loc[(bars.index.date == bars.index[-1].date()) & (bars.index.time <= time(9, 45))]
    avg_or_vol = float(or_seg["volume"].mean()) if not or_seg.empty else 0.0
    if avg_or_vol > 0 and vol < 1.2 * avg_or_vol:
        return Rejection(symbol, f"breakout volume {vol:.0f} < 1.2x OR avg {avg_or_vol:.0f}", NAME)

    rs = relative_strength(bars, index_bars)
    if rs < min_rs:
        return Rejection(symbol, f"weak vs NIFTY session ({rs:+.2%})", NAME)

    entry = close
    stop = round(min(or_range.low, low) - 0.05, 2)
    if stop <= 0 or entry <= stop:
        return Rejection(symbol, "invalid stop", NAME)
    risk = entry - stop
    target = round(entry + min_gross_rr * risk, 2)
    rr = (target - entry) / risk
    if rr < min_gross_rr - 1e-6:
        return Rejection(symbol, f"gross R/R {rr:.2f} < {min_gross_rr}", NAME)

    return Setup(
        symbol=symbol,
        sector=sector,
        entry=entry,
        stop=stop,
        target=target,
        score=50.0 + 100 * rs,
        evidence=[f"ORB above {or_range.high:.2f}", f"session RS {rs:+.2%}"],
        invalidation=f"close back below OR high {or_range.high:.2f}",
        strategy=NAME,
    )
