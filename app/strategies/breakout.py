"""Strategy A: breakout from an established base, with volume, trend and relative-strength confirmation."""
from __future__ import annotations
from app.strategies.common import MIN_BARS, Frame, Rejection, Setup, plan_target, score

NAME = "breakout"


def evaluate(symbol, sector, df, index_df, regime_points, frame: Frame | None = None, sector_rs=None, **_):
    if len(df) < MIN_BARS or len(index_df) < 70:
        return Rejection(symbol, "insufficient history", NAME)
    f = frame or Frame(df, index_df)
    if not f.uptrend():
        return Rejection(symbol, "no uptrend (close > SMA50 > SMA200, rising SMA50)", NAME)
    base = df.iloc[-26:-1]
    b_hi, b_lo = float(base["high"].max()), float(base["low"].min())
    depth = (b_hi - b_lo) / b_hi
    if not (0.04 <= depth <= 0.15):
        return Rejection(symbol, f"no base: 25-day range {depth:.1%} outside 4-15%", NAME)
    o = float(df["open"].iloc[-1])
    if not (f.close > b_hi and f.close > o):
        return Rejection(symbol, f"no breakout: close {f.close:.2f} not above base high {b_hi:.2f}", NAME)
    if f.close > b_hi * 1.04:
        return Rejection(symbol, f"extended {f.close / b_hi - 1:.1%} above base: not chasing", NAME)
    if f.rv < 1.5:
        return Rejection(symbol, f"breakout volume {f.rv:.2f}x < 1.5x average", NAME)
    if f.rs63 <= 0:
        return Rejection(symbol, f"weaker than NIFTY over 63 sessions ({f.rs63:+.1%})", NAME)
    entry = round(f.close, 2)
    stop = round(max(b_lo, b_hi - 1.5 * f.atr) - 0.25 * f.atr, 2)
    target, note = plan_target(f, entry, stop, measured=b_hi + (b_hi - b_lo))
    if target is None:
        return Rejection(symbol, note, NAME)
    s = score(f, regime_points, entry, stop, target, (f.rv - 1.0) / 1.5, sector_rs)
    ev = [f"base {b_lo:.2f}-{b_hi:.2f} ({depth:.1%} deep, 25 sessions)",
          f"breakout close {f.close:.2f} on {f.rv:.2f}x volume",
          f"trend: SMA50 {f.s50.iloc[-1]:.2f} > SMA200 {f.s200.iloc[-1]:.2f}, SMA50 10d {f.slope50:+.1%}",
          f"relative strength vs NIFTY (63d): {f.rs63:+.1%}", note]
    return Setup(symbol, sector, entry, stop, target, s, ev,
                 f"close back below {stop:.2f} (failed breakout), or next open > {entry * 1.005:.2f}", NAME)
