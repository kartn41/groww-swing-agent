"""Shared pieces for all strategies. Decisions use data up to bar t only (df ends at t)."""
from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd
from app.analysis.indicators import atr, ema, relative_volume, roc, rsi, sma
from app.analysis.levels import levels_as_of

MIN_BARS = 220


@dataclass
class Setup:
    symbol: str
    sector: str
    entry: float
    stop: float
    target: float
    score: float
    evidence: list[str] = field(default_factory=list)
    invalidation: str = ""
    strategy: str = ""

    @property
    def gross_rr(self) -> float:
        return (self.target - self.entry) / (self.entry - self.stop)


@dataclass
class Rejection:
    symbol: str
    reason: str
    strategy: str = ""


class Frame:
    """Indicators computed once per (symbol, day) and shared by every strategy."""

    def __init__(self, df: pd.DataFrame, index_df: pd.DataFrame):
        self.df, self.index_df = df, index_df
        c = df["close"]
        self.close = float(c.iloc[-1])
        self.s50, self.s200, self.e20 = sma(c, 50), sma(c, 200), ema(c, 20)
        self.atr = float(atr(df, 14).iloc[-1])
        self.rsi = float(rsi(c, 14).iloc[-1])
        self.rv = float(relative_volume(df["volume"], 20).iloc[-1])
        self.slope50 = float(self.s50.iloc[-1] / self.s50.iloc[-11] - 1)
        self.rs63 = float(roc(c, 63).iloc[-1] - roc(index_df["close"], 63).iloc[-1])
        self.rs20 = float(roc(c, 20).iloc[-1] - roc(index_df["close"], 20).iloc[-1])
        self.adv_value = float((c * df["volume"]).iloc[-20:].mean())
        self._levels = None

    @property
    def levels(self):
        if self._levels is None:
            self._levels = levels_as_of(self.df, len(self.df) - 1, self.atr)
        return self._levels

    def uptrend(self) -> bool:
        return (self.close > self.s50.iloc[-1] > self.s200.iloc[-1]) and self.slope50 > 0


def plan_target(f: Frame, entry: float, stop: float, measured: float | None, min_gross: float = 2.2,
                max_atr: float = 3.0):
    """Target = just below the first resistance zone above entry, else a measured move / 2.5R.
    Returns (target, note) or (None, reason)."""
    risk = entry - stop
    a = f.atr
    if not (0.75 * a <= risk <= max_atr * a) or risk / entry > 0.08:
        return None, f"stop distance {risk / entry:.1%} ({risk / a:.1f} ATR) not sensible"
    overhead = [z for z in f.levels.resistance if z.low > entry + 0.5 * a]
    if overhead:
        z = overhead[0]
        target, note = round(z.low - 0.1 * a, 2), f"target below resistance {z.low:.2f}-{z.high:.2f} ({z.touches} touches)"
        if measured is not None:
            target = round(min(target, measured), 2)
    elif measured is not None:
        target, note = round(measured, 2), "no overhead resistance: measured-move target"
    else:
        target, note = round(entry + 2.5 * risk, 2), "no overhead resistance: 2.5R target"
    rr = (target - entry) / risk
    if rr < min_gross:
        return None, f"gross reward/risk {rr:.2f} < {min_gross} before costs"
    return target, note


def score(f: Frame, regime_points: float, entry: float, stop: float, target: float,
          volume_quality: float, sector_rs: float | None) -> float:
    """0-100. Weights from spec section 10 (hypotheses to calibrate, not proven values)."""
    trend = 20 * min(1.0, max(0.0, f.slope50 / 0.03))
    rs_part = 0.7 * min(1.0, max(0.0, f.rs63 / 0.10)) + 0.3 * (1.0 if (sector_rs or 0) > 0 else 0.0)
    rr = 15 * min(1.0, max(0.0, ((target - entry) / (entry - stop) - 2.0) / 1.5))
    mom = 10 * (1.0 if 50 <= f.rsi <= 70 else 0.5)
    liq = 10 * min(1.0, f.adv_value / 5e8)
    return round(regime_points + trend + 15 * rs_part + 15 * max(0.0, min(1.0, volume_quality)) + mom + rr + liq, 1)
