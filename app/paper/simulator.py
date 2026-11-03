"""Fill rules on daily bars. Deliberately conservative (paper results are an upper bound anyway)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


def entry_fill(bar: Bar, limit: float, max_chase_pct: float) -> tuple[Optional[float], str]:
    """BUY LIMIT for this session. Returns (fill_price or None, note)."""
    if bar.open > limit * (1 + max_chase_pct / 100):
        return None, f"opened {bar.open:.2f} > limit {limit:.2f} + {max_chase_pct}%: not chasing"
    if bar.open <= limit:
        return bar.open, "filled at open (at/below limit)"
    if bar.low <= limit:
        return limit, "filled at limit intraday"
    return None, "limit not reached"


def exit_check(bar: Bar, stop: float, target: float, entry_day: bool) -> tuple[Optional[str], Optional[float]]:
    """Order of events inside a daily bar is unknown => assume the worst for us."""
    if not entry_day:
        if bar.open <= stop:
            return "STOP_GAP", bar.open          # gapped through the stop: exit at the open, worse
        if bar.open >= target:
            return "TARGET_GAP", bar.open
    if bar.low <= stop:
        return "STOP", stop                       # stop assumed first if both were touched
    if not entry_day and bar.high >= target:
        return "TARGET", target                   # entry-day target hits are not credited
    return None, None
