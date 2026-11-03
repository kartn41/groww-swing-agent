"""Market regime from the NIFTY index + breadth. Unknown data => UNKNOWN => no new longs."""
from __future__ import annotations
from dataclasses import dataclass
import pandas as pd
from app.analysis.indicators import atr, sma

LONG_OK = {"STRONG_UP", "WEAK_UP"}


@dataclass
class Regime:
    name: str
    detail: str
    breadth_pct: float | None = None

    @property
    def allows_new_longs(self) -> bool:
        return self.name in LONG_OK


def classify(index_df: pd.DataFrame, breadth_pct: float | None) -> Regime:
    if index_df is None or len(index_df) < 220 or breadth_pct is None:
        return Regime("UNKNOWN", "insufficient index or breadth data", breadth_pct)
    c = index_df["close"]
    s50, s200 = sma(c, 50), sma(c, 200)
    a = atr(index_df, 14) / c
    close, m50, m200 = c.iloc[-1], s50.iloc[-1], s200.iloc[-1]
    slope50 = s50.iloc[-1] / s50.iloc[-21] - 1
    vol_now, vol_med = a.iloc[-1], a.iloc[-250:].median()
    detail = (f"NIFTY {close:.0f}, SMA50 {m50:.0f}, SMA200 {m200:.0f}, SMA50 20d slope {slope50:+.2%}, "
              f"ATR% {vol_now:.2%} (1y median {vol_med:.2%}), breadth {breadth_pct:.0f}% above SMA50")
    if vol_now > 2 * vol_med:
        return Regime("HIGH_VOLATILITY", detail, breadth_pct)
    if close > m50 > m200 and slope50 > 0 and breadth_pct >= 60:
        return Regime("STRONG_UP", detail, breadth_pct)
    if close > m200 and (close > m50 or slope50 > 0) and breadth_pct >= 45:
        return Regime("WEAK_UP", detail, breadth_pct)
    if close < m50 < m200 and slope50 < 0 and breadth_pct < 35:
        return Regime("STRONG_DOWN", detail, breadth_pct)
    if close < m200:
        return Regime("WEAK_DOWN", detail, breadth_pct)
    return Regime("SIDEWAYS", detail, breadth_pct)


def breadth(stock_frames: dict[str, pd.DataFrame]) -> float | None:
    above = total = 0
    for df in stock_frames.values():
        if len(df) >= 50:
            m = sma(df["close"], 50).iloc[-1]
            total += 1
            above += int(df["close"].iloc[-1] > m)
    return 100 * above / total if total >= 20 else None
