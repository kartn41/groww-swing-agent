"""Data-quality checks. 'issues' block use of the symbol; 'warnings' are reported only."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
import numpy as np
import pandas as pd


@dataclass
class Quality:
    ok: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_daily(df: pd.DataFrame, as_of: date, min_bars: int = 220,
                   max_overnight_jump: float = 0.35) -> Quality:
    q = Quality(ok=True)
    if df.empty:
        q.issues.append("no data")
    else:
        if df.index[-1] != as_of:
            q.issues.append(f"no candle for {as_of} (last {df.index[-1]})")
        if len(df) < min_bars:
            q.issues.append(f"only {len(df)} bars (< {min_bars})")
        px = df[["open", "high", "low", "close"]]
        if (px <= 0).any().any() or px.isna().any().any():
            q.issues.append("non-positive or missing prices")
        bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
        if bad.any():
            q.issues.append(f"{int(bad.sum())} candles with inconsistent high/low")
        if not df.index.is_monotonic_increasing or df.index.has_duplicates:
            q.issues.append("dates not strictly increasing")
        recent = df.iloc[-260:]
        jump = (recent["open"] / recent["close"].shift(1) - 1).abs()
        if (jump > max_overnight_jump).any():
            d = jump.idxmax()
            q.issues.append(f"overnight move {jump.max():.0%} on {d}: possible unadjusted split/bonus")
        idx = pd.to_datetime(pd.Index(df.index))
        gaps = np.diff(idx.values).astype("timedelta64[D]").astype(int)
        if len(gaps) and gaps[-260:].max(initial=0) > 6:
            q.warnings.append(f"gap of {gaps[-260:].max()} calendar days in last year")
        if (df["volume"].iloc[-20:] <= 0).any():
            q.warnings.append("zero-volume days in last 20 sessions")
    q.ok = not q.issues
    return q
