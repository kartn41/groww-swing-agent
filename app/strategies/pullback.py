"""Strategy B from the spec: trend pullback.

Decision is made on the CLOSE of bar t (all data <= t). The order is for the NEXT session.
Every threshold below is an initial hypothesis to be validated by backtesting, not a proven value.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
import pandas as pd
from app.analysis.indicators import atr, ema, relative_volume, roc, rsi, sma
from app.analysis.levels import levels_as_of

NAME = "pullback"
MIN_BARS = 220
BOUNCE_MIN_RV = 1.0        # bounce-day volume vs the previous 20 days
DIP_MAX_VOL_RATIO = 1.0    # dip-day volume vs the 20 days before the dip


def volume_gate_on() -> bool:
    return os.environ.get("PULLBACK_VOLUME_GATE", "off").strip().lower() in ("on", "true", "1")


from app.strategies.common import Rejection, Setup  # noqa: E402  (shared types)


def evaluate(symbol: str, sector: str, df: pd.DataFrame, index_df: pd.DataFrame,
             regime_points: float, **_ctx) -> Setup | Rejection:
    """df / index_df must END at the decision bar t. regime_points: 0-15 from the regime."""
    if len(df) < MIN_BARS or len(index_df) < 70:
        return Rejection(symbol, "insufficient history")
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]
    t = len(df) - 1
    s50, s200, e20 = sma(c, 50), sma(c, 200), ema(c, 20)
    a = atr(df, 14).iloc[-1]
    r = rsi(c, 14).iloc[-1]
    close = float(c.iloc[-1])

    # 1. Established uptrend
    slope50 = s50.iloc[-1] / s50.iloc[-11] - 1
    if not (close > s200.iloc[-1] and s50.iloc[-1] > s200.iloc[-1] and slope50 > 0):
        return Rejection(symbol, "no established uptrend (close>SMA200, SMA50>SMA200, rising SMA50)")
    if close <= s50.iloc[-1]:
        # The exit rule sells on a close below SMA50; entering below it would be an instant exit.
        return Rejection(symbol, "close below SMA50 (would trigger the trend-break exit immediately)")

    # 2. Controlled pullback in the last 5 bars, toward EMA20 or a support zone
    hi20 = float(h.iloc[-20:].max())
    pb_low = float(l.iloc[-5:].min())
    depth = (hi20 - pb_low) / hi20
    if not (0.03 <= depth <= 0.12):
        return Rejection(symbol, f"pullback depth {depth:.1%} outside 3-12%")
    lv = levels_as_of(df, t, a)
    near_ema = abs(pb_low - e20.iloc[-1]) <= 1.0 * a
    near_support = any(z.low - 0.5 * a <= pb_low <= z.high + 0.5 * a for z in lv.support[:3])
    if not (near_ema or near_support):
        return Rejection(symbol, "pullback did not reach EMA20 or a support zone")

    # 3. Selling pressure easing + bullish confirmation on bar t
    if not (close > float(h.iloc[-2]) and close > float(o.iloc[-1])):
        return Rejection(symbol, "no confirmation (close above prior high on an up candle)")
    if not (40 <= r <= 70):
        return Rejection(symbol, f"RSI {r:.0f} outside 40-70")
    rv = relative_volume(v, 20)
    base_vol = v.iloc[-25:-5].mean()
    pb_vol_ratio = float(v.iloc[-5:-1].mean() / base_vol) if base_vol > 0 else 1.0
    if volume_gate_on():
        if not float(rv.iloc[-1]) >= BOUNCE_MIN_RV:
            return Rejection(symbol, f"bounce volume {float(rv.iloc[-1]):.2f}x < {BOUNCE_MIN_RV}x average")
        if not pb_vol_ratio < DIP_MAX_VOL_RATIO:
            return Rejection(symbol, f"selling volume did not dry up during the dip ({pb_vol_ratio:.2f}x prior average)")

    # 4. Relative strength vs NIFTY (3 months)
    rs = float(roc(c, 63).iloc[-1] - roc(index_df["close"], 63).iloc[-1])
    if rs <= 0:
        return Rejection(symbol, f"weaker than NIFTY over 63 sessions ({rs:+.1%})")

    # 5. Trade plan from structure
    entry = round(close, 2)
    stop = round(pb_low - 0.25 * a, 2)
    risk = entry - stop
    if not (0.75 * a <= risk <= 3.0 * a) or risk / entry > 0.08:
        return Rejection(symbol, f"stop distance {risk / entry:.1%} ({risk / a:.1f} ATR) not sensible")
    overhead = [z for z in lv.resistance if z.low > entry + 0.5 * a]
    if overhead:
        target = round(overhead[0].low - 0.1 * a, 2)
        tgt_note = f"target below resistance zone {overhead[0].low:.2f}-{overhead[0].high:.2f} ({overhead[0].touches} touches)"
    else:
        target = round(entry + 2.5 * risk, 2)
        tgt_note = "no overhead resistance found: measured 2.5R target"
    if (target - entry) / risk < 2.2:
        return Rejection(symbol, f"gross reward/risk {(target - entry) / risk:.2f} < 2.2 before costs")

    # 6. Score (0-100). Weights are hypotheses (spec section 10); calibrate via backtests.
    trend_pts = 20 * min(1.0, max(0.0, slope50 / 0.03))
    rs_pts = 15 * min(1.0, rs / 0.10)
    vol_pts = 7.5 * (1.0 if pb_vol_ratio < 1.0 else 0.0) + 7.5 * min(1.0, max(0.0, float(rv.iloc[-1]) - 0.5))
    mom_pts = 10 * (1.0 if 50 <= r <= 65 else 0.5)
    rr_pts = 15 * min(1.0, ((target - entry) / risk - 2.0) / 1.5)
    adv_value = float((c * v).iloc[-20:].mean())
    liq_pts = 10 * min(1.0, adv_value / 5e8)          # Rs 50 Cr/day => full points
    score = round(regime_points + trend_pts + rs_pts + vol_pts + mom_pts + rr_pts + liq_pts, 1)

    ev = [
        f"uptrend: close {close:.2f} > SMA50 {s50.iloc[-1]:.2f} > SMA200 {s200.iloc[-1]:.2f}, SMA50 10d {slope50:+.1%}",
        f"pullback {depth:.1%} from 20d high {hi20:.2f} to {pb_low:.2f} "
        + ("(at EMA20)" if near_ema else "") + (" (at support zone)" if near_support else ""),
        f"confirmation: close above prior high {float(h.iloc[-2]):.2f}; RSI {r:.0f}",
        f"relative strength vs NIFTY (63d): {rs:+.1%}",
        f"pullback volume {pb_vol_ratio:.2f}x prior average; today {float(rv.iloc[-1]):.2f}x",
        tgt_note,
    ]
    return Setup(symbol, sector, entry, stop, target, score, ev,
                 invalidation=f"daily close below {stop:.2f}, or next open > {entry * 1.005:.2f} (no chase)",
                 strategy=NAME)
