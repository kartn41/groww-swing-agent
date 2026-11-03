"""Strategy D (optional): volatility contraction. Three successively tighter ranges on falling
volume, then a breakout above the last contraction's high."""
from __future__ import annotations
from app.strategies.common import MIN_BARS, Frame, Rejection, Setup, plan_target, score

NAME = "vcp"


def evaluate(symbol, sector, df, index_df, regime_points, frame: Frame | None = None, sector_rs=None, **_):
    if len(df) < MIN_BARS or len(index_df) < 70:
        return Rejection(symbol, "insufficient history", NAME)
    f = frame or Frame(df, index_df)
    # A 45-session contraction flattens the 50-day average, so require a rising 200-day instead.
    slope200 = float(f.s200.iloc[-1] / f.s200.iloc[-21] - 1)
    if not (f.close > f.s50.iloc[-1] > f.s200.iloc[-1] and slope200 > 0):
        return Rejection(symbol, "no uptrend (close > SMA50 > SMA200, rising SMA200)", NAME)
    w = [df.iloc[-46:-31], df.iloc[-31:-16], df.iloc[-16:-1]]
    rng = [(float(x["high"].max()) - float(x["low"].min())) / float(x["high"].max()) for x in w]
    if not (rng[0] > rng[1] > rng[2] and rng[2] <= 0.08 and rng[0] >= 1.5 * rng[2]):
        return Rejection(symbol, "no volatility contraction (" + " > ".join(f"{r:.1%}" for r in rng) + " required)", NAME)
    if not float(w[2]["volume"].mean()) < float(w[0]["volume"].mean()):
        return Rejection(symbol, "volume not drying up during contraction", NAME)
    pivot = float(w[2]["high"].max())
    if not (f.close > pivot and f.rv >= 1.3):
        return Rejection(symbol, f"no pivot breakout above {pivot:.2f} on >=1.3x volume", NAME)
    if f.close > pivot * 1.03:
        return Rejection(symbol, f"extended {f.close / pivot - 1:.1%} above pivot: not chasing", NAME)
    entry = round(f.close, 2)
    stop = round(float(w[2]["low"].min()) - 0.25 * f.atr, 2)
    # ATR shrinks during a contraction, so the structural stop is often 3-5 ATR; the 8% cap still applies.
    target, note = plan_target(f, entry, stop, measured=None, max_atr=5.0)
    if target is None:
        return Rejection(symbol, note, NAME)
    s = score(f, regime_points, entry, stop, target, (f.rv - 1.0) / 1.0, sector_rs)
    ev = ["contractions " + " > ".join(f"{r:.1%}" for r in rng) + " with falling volume",
          f"pivot breakout above {pivot:.2f} on {f.rv:.2f}x volume", note]
    return Setup(symbol, sector, entry, stop, target, s, ev,
                 f"close below {stop:.2f}, or next open > {entry * 1.005:.2f}", NAME)
