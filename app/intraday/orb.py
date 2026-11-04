"""Opening-range breakout (long-only), decided on the close of a completed 5m bar.
`bars` / `index_bars` hold the current session only, ending at the signal bar."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import time
import pandas as pd

from app.intraday.config import IntradayConfig
from app.strategies.common import Rejection, Setup

NAME = "INTRADAY_ORB"
NO_BREAKOUT = "no breakout"


@dataclass
class OpeningRange:
    high: float
    low: float
    ready: bool

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


def opening_range(bars: pd.DataFrame, or_end: time, min_bars: int = 5) -> OpeningRange:
    """OR = session bars starting before or_end. Not ready until min_bars of them exist."""
    if bars.empty:
        return OpeningRange(0.0, 0.0, False)
    seg = bars.loc[bars.index.time < or_end]
    if len(seg) < min_bars:
        return OpeningRange(0.0, 0.0, False)
    return OpeningRange(float(seg["high"].max()), float(seg["low"].min()), True)


def session_vwap(bars: pd.DataFrame) -> float | None:
    vol = bars["volume"]
    if float(vol.sum()) <= 0:
        return None
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    return float((typical * vol).sum() / vol.sum())


def session_return(bars: pd.DataFrame) -> float:
    return float(bars["close"].iloc[-1] / bars["open"].iloc[0] - 1)


def index_ok(index_bars: pd.DataFrame, cfg: IntradayConfig) -> tuple[bool, str]:
    """Long breakouts only when NIFTY is above its open and its opening-range midpoint."""
    if not cfg.require_index_green:
        return True, "index filter off"
    orr = opening_range(index_bars, cfg.or_end)
    if not orr.ready:
        return False, "NIFTY opening range not ready"
    last = float(index_bars["close"].iloc[-1])
    first_open = float(index_bars["open"].iloc[0])
    if last < first_open:
        return False, f"NIFTY below its open ({last / first_open - 1:+.2%})"
    if last < orr.mid:
        return False, f"NIFTY below its opening-range midpoint {orr.mid:.2f}"
    return True, f"NIFTY {last / first_open - 1:+.2%} on the day, above OR midpoint"


def evaluate_orb(symbol: str, sector: str, bars: pd.DataFrame, index_bars: pd.DataFrame,
                 cfg: IntradayConfig) -> Setup | Rejection:
    orr = opening_range(bars, cfg.or_end)
    if not orr.ready:
        return Rejection(symbol, "opening range not ready", NAME)
    after_or = bars.loc[bars.index.time >= cfg.or_end]
    if after_or.empty:
        return Rejection(symbol, "no bars after the opening range", NAME)

    last = bars.iloc[-1]
    o, h, l, c, v = (float(last[k]) for k in ("open", "high", "low", "close", "volume"))
    if c <= orr.high:
        return Rejection(symbol, NO_BREAKOUT, NAME)
    prev_after = after_or.iloc[:-1]
    if not prev_after.empty and float(prev_after["close"].max()) > orr.high:
        return Rejection(symbol, "not the first close above OR high (late)", NAME)

    width = (orr.high - orr.low) / orr.low * 100
    if not cfg.min_or_pct <= width <= cfg.max_or_pct:
        return Rejection(symbol, f"opening range {width:.2f}% outside {cfg.min_or_pct}-{cfg.max_or_pct}%", NAME)
    ext = (c / orr.high - 1) * 100
    if ext > cfg.max_extension_pct:
        return Rejection(symbol, f"extended {ext:.2f}% above OR high: not chasing", NAME)
    if c <= o:
        return Rejection(symbol, "breakout bar is not green", NAME)
    close_pos = (c - l) / (h - l) if h > l else 1.0
    if close_pos < cfg.min_close_position:
        return Rejection(symbol, f"weak close ({close_pos:.0%} of bar range)", NAME)

    prior = bars.iloc[:-1]
    avg_vol = float(prior["volume"].mean()) if not prior.empty else 0.0
    vol_x = v / avg_vol if avg_vol > 0 else 0.0
    if vol_x < cfg.volume_mult:
        return Rejection(symbol, f"breakout volume {vol_x:.2f}x < {cfg.volume_mult}x session average", NAME)
    turnover = float((bars["close"] * bars["volume"]).mean())
    if turnover < cfg.min_bar_turnover:
        return Rejection(symbol, f"thin: Rs{turnover / 1e5:.0f} lakh per bar", NAME)

    vwap = session_vwap(bars)
    if cfg.require_vwap and vwap is not None and c <= vwap:
        return Rejection(symbol, f"close {c:.2f} not above VWAP {vwap:.2f}", NAME)

    rs = (session_return(bars) - session_return(index_bars)) * 100
    if rs < cfg.min_rs_pct:
        return Rejection(symbol, f"relative strength {rs:+.2f}% < {cfg.min_rs_pct}% vs NIFTY", NAME)

    entry = c
    stop = round(max(orr.mid, l) - 0.0005 * entry, 2)
    risk_pct = (entry - stop) / entry * 100
    if risk_pct < cfg.min_risk_pct:
        return Rejection(symbol, f"stop too tight ({risk_pct:.2f}%)", NAME)
    if risk_pct > cfg.max_risk_pct:
        return Rejection(symbol, f"stop too wide ({risk_pct:.2f}% > {cfg.max_risk_pct}%)", NAME)
    target = round(entry + cfg.target_r * (entry - stop), 2)

    score = round(40 * min(1.0, rs / 1.0) + 30 * min(1.0, (vol_x - 1) / 2) + 30 * close_pos, 1)
    if score < cfg.min_score:
        return Rejection(symbol, f"score {score} < {cfg.min_score}", NAME)

    evidence = [
        f"First close above opening range high {orr.high:.2f} (range {width:.2f}% wide)",
        f"Breakout volume {vol_x:.1f}x session average, closed at {close_pos:.0%} of the bar",
        f"Stronger than NIFTY by {rs:+.2f}% since the open",
    ]
    if vwap is not None:
        evidence.append(f"Above VWAP {vwap:.2f}")
    return Setup(symbol=symbol, sector=sector, entry=entry, stop=stop, target=target, score=score,
                 evidence=evidence,
                 invalidation=f"falls back to {stop:.2f} (opening-range midpoint / breakout bar low)",
                 strategy=NAME)
