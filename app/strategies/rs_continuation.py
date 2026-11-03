"""Strategy C: relative-strength leader resuming its move after a tight pause."""
from __future__ import annotations
from app.strategies.common import MIN_BARS, Frame, Rejection, Setup, plan_target, score

NAME = "rs_continuation"


def evaluate(symbol, sector, df, index_df, regime_points, frame: Frame | None = None, sector_rs=None, **_):
    if len(df) < MIN_BARS or len(index_df) < 70:
        return Rejection(symbol, "insufficient history", NAME)
    f = frame or Frame(df, index_df)
    if not (f.uptrend() and f.close > f.e20.iloc[-1]):
        return Rejection(symbol, "no uptrend above EMA20", NAME)
    if f.rs63 < 0.05 or f.rs20 <= 0:
        return Rejection(symbol, f"not a relative-strength leader (63d {f.rs63:+.1%}, 20d {f.rs20:+.1%})", NAME)
    if sector_rs is not None and sector_rs <= 0:
        return Rejection(symbol, f"sector lagging NIFTY ({sector_rs:+.1%})", NAME)
    pause = df.iloc[-11:-1]
    p_hi, p_lo = float(pause["high"].max()), float(pause["low"].min())
    if p_hi - p_lo > 3.0 * f.atr:
        return Rejection(symbol, f"pause not tight ({(p_hi - p_lo) / f.atr:.1f} ATR > 3)", NAME)
    if not (f.close > p_hi and f.rv >= 1.2):
        return Rejection(symbol, "no resumption (close above 10-day pause high on >=1.2x volume)", NAME)
    entry = round(f.close, 2)
    stop = round(p_lo - 0.25 * f.atr, 2)
    target, note = plan_target(f, entry, stop, measured=None)
    if target is None:
        return Rejection(symbol, note, NAME)
    s = score(f, regime_points, entry, stop, target, (f.rv - 1.0) / 1.0, sector_rs)
    ev = [f"relative strength vs NIFTY: 63d {f.rs63:+.1%}, 20d {f.rs20:+.1%}",
          f"sector vs NIFTY (63d): {sector_rs:+.1%}" if sector_rs is not None else "sector strength unavailable",
          f"10-day pause {p_lo:.2f}-{p_hi:.2f} ({(p_hi - p_lo) / f.atr:.1f} ATR), resumed on {f.rv:.2f}x volume",
          note]
    return Setup(symbol, sector, entry, stop, target, s, ev,
                 f"close below {stop:.2f}, or next open > {entry * 1.005:.2f}", NAME)
