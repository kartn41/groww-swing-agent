"""Daily technical picture of one ETF. Every value at bar t uses bars <= t only (df must END at t,
and t must be a completed session)."""
from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd

from app.analysis.indicators import adx, atr, bollinger, ema, macd, relative_volume, roc, rsi

MIN_BARS = 220
STRONG_BULL, MILD_BULL, SIDEWAYS, MILD_BEAR, STRONG_BEAR = (
    "STRONG_BULLISH", "MILD_BULLISH", "SIDEWAYS", "MILD_BEARISH", "STRONG_BEARISH")
REGIMES = (STRONG_BULL, MILD_BULL, SIDEWAYS, MILD_BEAR, STRONG_BEAR)
# pullback types
NO_PULLBACK = "NO_PULLBACK"
UPTREND_PULLBACK = "PULLBACK_IN_UPTREND"
HEALTHY_CORRECTION = "HEALTHY_CORRECTION"
BEARISH_MOMENTUM = "STRONG_BEARISH_MOMENTUM"
POSSIBLE_REVERSAL = "POSSIBLE_REVERSAL"


@dataclass
class Snapshot:
    symbol: str
    d: object
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    prev_high: float
    ema: dict                    # {5: .., 9: .., 20: .., 50: .., 200: ..}
    slope50: float               # EMA50 change over 10 sessions
    adx: float
    pdi: float
    mdi: float
    rsi: float
    rsi_prev: float
    rsi_min5: float
    macd: float
    macd_signal: float
    hist: float
    hist_prev: float
    hist_prev2: float
    roc10: float
    rv: float                    # volume vs previous 20-session average
    atr: float
    atr_pct: float
    atr_pct_median: float        # 1-year median of ATR%
    bb_lower: float
    bb_upper: float
    pct_b: float
    moves: dict                  # {2: .., 3: .., 5: .., 10: ..} close-to-close returns
    swing_high: float            # highest high of the last 20 sessions
    pullback_low: float          # lowest low of the last 5 sessions
    depth_pct: float             # (swing_high - pullback_low) / swing_high
    depth_atr: float
    regime: str = SIDEWAYS
    regime_detail: str = ""
    pullback: str = NO_PULLBACK
    confirmations: list = field(default_factory=list)
    vol_shock: bool = False

    @property
    def bull(self) -> bool:
        return self.regime in (STRONG_BULL, MILD_BULL)

    def summary(self) -> str:
        e = self.ema
        return (f"close {self.close:.2f} | EMA5/9/20/50/200 {e[5]:.2f}/{e[9]:.2f}/{e[20]:.2f}/{e[50]:.2f}/{e[200]:.2f} | "
                f"ADX {self.adx:.0f} (+DI {self.pdi:.0f} / -DI {self.mdi:.0f}) | RSI {self.rsi:.0f} | "
                f"MACD hist {self.hist:+.3f} | ROC10 {self.roc10:+.1%} | ATR {self.atr:.2f} ({self.atr_pct:.2%}) | "
                f"BB %b {self.pct_b:.2f} | vol {self.rv:.2f}x | 2/3/5/10d "
                + "/".join(f"{self.moves[k]:+.1%}" for k in (2, 3, 5, 10)))


class InsufficientData(Exception):
    pass


def sanitize(df: pd.DataFrame, k_open: float = 3.0, k_wick: float = 2.5) -> tuple[pd.DataFrame, int]:
    """Repair isolated bad prints (an open or wick far outside an otherwise normal bar), which inflate ATR and
    trigger stops at prices nobody traded. Real gaps survive: a gap open is followed by prices near it.
    Scale m = median high-low range of the previous 20 bars (past data only). Returns (clean df, bars changed)."""
    out = df.copy()
    m = (df["high"] - df["low"]).shift(1).rolling(20, min_periods=5).median()
    pc = df["close"].shift(1)
    o, h, l, c = (df[x].to_numpy(copy=True) for x in ("open", "high", "low", "close"))
    mv, pcv = m.to_numpy(), pc.to_numpy()
    changed = 0
    for i in range(len(df)):
        if not (mv[i] > 0) or pcv[i] != pcv[i]:
            continue
        lim = mv[i]
        new_o = o[i]
        if abs(o[i] - pcv[i]) > k_open * lim and abs(o[i] - c[i]) > k_open * lim and abs(c[i] - pcv[i]) <= 1.5 * lim:
            new_o = pcv[i]
        body_hi, body_lo = max(new_o, c[i]), min(new_o, c[i])
        new_h = min(h[i], body_hi + k_wick * lim) if h[i] > body_hi + k_wick * lim and abs(c[i] - pcv[i]) <= 1.5 * lim else h[i]
        new_l = max(l[i], body_lo - k_wick * lim) if l[i] < body_lo - k_wick * lim and abs(c[i] - pcv[i]) <= 1.5 * lim else l[i]
        new_h, new_l = max(new_h, body_hi), min(new_l, body_lo)
        if (new_o, new_h, new_l) != (o[i], h[i], l[i]):
            changed += 1
            o[i], h[i], l[i] = new_o, new_h, new_l
    out["open"], out["high"], out["low"] = o, h, l
    return out, changed


def analyse(symbol: str, df: pd.DataFrame) -> Snapshot:
    if len(df) < MIN_BARS:
        raise InsufficientData(f"{symbol}: {len(df)} daily bars, need {MIN_BARS}")
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]
    emas = {n: ema(c, n) for n in (5, 9, 20, 50, 200)}
    a = atr(df, 14)
    dmi, m, bb, r = adx(df, 14), macd(c), bollinger(c), rsi(c, 14)
    atr_pct = a / c
    close = float(c.iloc[-1])
    hi20, lo5 = float(h.iloc[-20:].max()), float(l.iloc[-5:].min())
    s = Snapshot(
        symbol=symbol, d=df.index[-1], open=float(o.iloc[-1]), high=float(h.iloc[-1]), low=float(l.iloc[-1]),
        close=close, prev_close=float(c.iloc[-2]), prev_high=float(h.iloc[-2]),
        ema={n: float(x.iloc[-1]) for n, x in emas.items()},
        slope50=float(emas[50].iloc[-1] / emas[50].iloc[-11] - 1),
        adx=float(dmi["adx"].iloc[-1]), pdi=float(dmi["pdi"].iloc[-1]), mdi=float(dmi["mdi"].iloc[-1]),
        rsi=float(r.iloc[-1]), rsi_prev=float(r.iloc[-2]), rsi_min5=float(r.iloc[-5:].min()),
        macd=float(m["macd"].iloc[-1]), macd_signal=float(m["signal"].iloc[-1]),
        hist=float(m["hist"].iloc[-1]), hist_prev=float(m["hist"].iloc[-2]), hist_prev2=float(m["hist"].iloc[-3]),
        roc10=float(roc(c, 10).iloc[-1]), rv=float(relative_volume(v, 20).iloc[-1]),
        atr=float(a.iloc[-1]), atr_pct=float(atr_pct.iloc[-1]), atr_pct_median=float(atr_pct.iloc[-250:].median()),
        bb_lower=float(bb["lower"].iloc[-1]), bb_upper=float(bb["upper"].iloc[-1]), pct_b=float(bb["pct_b"].iloc[-1]),
        moves={k: float(c.iloc[-1] / c.iloc[-1 - k] - 1) for k in (2, 3, 5, 10)},
        swing_high=hi20, pullback_low=lo5, depth_pct=(hi20 - lo5) / hi20,
        depth_atr=(hi20 - lo5) / float(a.iloc[-1]) if float(a.iloc[-1]) > 0 else 0.0,
    )
    s.vol_shock = s.atr_pct > 2.0 * s.atr_pct_median
    s.regime, s.regime_detail = classify_regime(s)
    s.confirmations = reversal_confirmations(s)
    s.pullback = classify_pullback(s)
    return s


def classify_regime(s: Snapshot) -> tuple[str, str]:
    e = s.ema
    detail = (f"EMA20 {e[20]:.2f} / EMA50 {e[50]:.2f} / EMA200 {e[200]:.2f}, EMA50 10d {s.slope50:+.1%}, "
              f"ADX {s.adx:.0f}, +DI {s.pdi:.0f} / -DI {s.mdi:.0f}")
    if e[20] < e[50] and s.close < e[50] and s.slope50 < 0 and s.mdi > s.pdi and s.adx >= 20:
        return STRONG_BEAR, detail
    if e[50] < e[200] and s.close < e[200] and s.slope50 < 0:
        return STRONG_BEAR, detail
    if e[20] > e[50] > e[200] and s.slope50 > 0 and s.adx >= 20 and s.close > e[50]:
        return STRONG_BULL, detail
    if s.adx < 18 and abs(s.slope50) < 0.01:
        return SIDEWAYS, detail
    if s.close < e[200] or (e[20] < e[50] and s.close < e[50]):
        return MILD_BEAR, detail
    if s.close > e[200] and e[50] > e[200]:
        return MILD_BULL, detail
    return SIDEWAYS, detail


def reversal_confirmations(s: Snapshot) -> list[str]:
    """Evidence that selling is easing / buyers are back, on the decision bar."""
    out = []
    rng = s.high - s.low
    if s.hist > s.hist_prev:
        out.append(f"MACD histogram rising ({s.hist_prev:+.3f} -> {s.hist:+.3f}): selling momentum weakening")
    if s.hist > s.hist_prev > s.hist_prev2 or (s.macd > s.macd_signal and s.hist_prev <= 0):
        out.append("MACD bullish turn (2 rising bars or fresh crossover)")
    if s.close > s.prev_high or (s.close > s.open and rng > 0 and s.close >= s.low + 0.6 * rng):
        out.append("bullish reversal candle (strong close / above prior high)")
    if s.rsi > s.rsi_prev and s.rsi >= s.rsi_min5 + 3:
        out.append(f"RSI recovering ({s.rsi_min5:.0f} -> {s.rsi:.0f})")
    if s.close > s.prev_close and s.rv >= 1.0:
        out.append(f"up day on {s.rv:.1f}x average volume")
    if s.close > s.ema[5]:
        out.append(f"reclaimed EMA5 {s.ema[5]:.2f}")
    return out


def _selling_strong(s: Snapshot) -> bool:
    rng = s.high - s.low
    near_low = rng > 0 and s.close <= s.low + 0.3 * rng
    return s.hist < s.hist_prev and s.moves[5] < 0 and near_low and s.mdi > s.pdi


def classify_pullback(s: Snapshot) -> str:
    if s.depth_atr < 1.0:
        return NO_PULLBACK
    if _selling_strong(s):
        return BEARISH_MOMENTUM
    n = len(s.confirmations)
    if s.regime == STRONG_BEAR:
        return POSSIBLE_REVERSAL if (n >= 4 and s.rsi_min5 < 35) else BEARISH_MOMENTUM
    if s.regime == MILD_BEAR:
        return POSSIBLE_REVERSAL if n >= 3 else BEARISH_MOMENTUM
    if s.bull and s.pullback_low >= s.ema[50] - 0.5 * s.atr and s.depth_atr <= 3.5:
        return UPTREND_PULLBACK
    if s.close >= s.ema[200]:
        return HEALTHY_CORRECTION
    return BEARISH_MOMENTUM
