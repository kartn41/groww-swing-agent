"""Groww 5-minute historical candles (same API as daily, different interval + parser)."""
from __future__ import annotations
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.data.groww_source import GrowwCandleSource, FatalDataError, MalformedResponse, RateLimited, TransientFailure, classify

IST = ZoneInfo("Asia/Kolkata")
FIVE_MIN_INTERVAL_NAMES = (
    "CANDLE_INTERVAL_5_MINUTE",
    "CANDLE_INTERVAL_FIVE_MINUTE",
    "CANDLE_INTERVAL_5MIN",
    "CANDLE_INTERVAL_I5",
)


def parse_intraday_candles(resp: dict, skipped: list | None = None) -> list[tuple]:
    """Rows: (d 'YYYY-MM-DD HH:MM:SS', o, h, l, c, v) in IST."""
    rows = []
    for c in (resp or {}).get("candles") or []:
        label = "?"
        try:
            ts = c[0]
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, IST)
            else:
                s = str(ts).replace("T", " ")[:19]
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST)
                else:
                    dt = dt.astimezone(IST)
            label = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            if skipped is not None:
                skipped.append((label, f"unreadable timestamp {str(c)[:60]}"))
            continue
        vals = [_num(x) for x in c[1:5]]
        if len(vals) < 4 or None in vals or min(vals) <= 0:
            if skipped is not None:
                skipped.append((label, f"invalid prices {list(c[1:5])}"))
            continue
        v = _num(c[5]) if len(c) > 5 else 0.0
        rows.append((label, vals[0], vals[1], vals[2], vals[3], max(v, 0.0) if v is not None else 0.0))
    return rows


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class GrowwIntradaySource(GrowwCandleSource):
    def __init__(self, groww, **kw):
        super().__init__(groww, **kw)
        self.interval = next((getattr(groww, n) for n in FIVE_MIN_INTERVAL_NAMES if hasattr(groww, n)), None)
        if self.interval is None:
            raise RuntimeError(f"growwapi has none of {FIVE_MIN_INTERVAL_NAMES}; check Groww annexures")

    def iter_bars(self, groww_symbol: str, start: date, end: date):
        for s, e, resp in self.iter_raw(groww_symbol, start, end):
            skipped: list = []
            rows = parse_intraday_candles(resp, skipped)
            bad, total = len(skipped), len((resp or {}).get("candles") or [])
            if total >= 20 and bad > total * 0.5:
                raise MalformedResponse(f"{bad}/{total} unusable 5m candles for {groww_symbol} {s}..{e}")
            yield rows


def sync_intraday(store, source: GrowwIntradaySource, symbols: list[str], start: date, end: date) -> dict:
    summary = {"symbols": 0, "rows": 0, "errors": {}}
    for sym in symbols:
        try:
            n = 0
            for chunk in source.iter_bars(sym, start, end):
                n += store.upsert(sym, chunk)
            summary["symbols"] += 1
            summary["rows"] += n
        except (FatalDataError, RateLimited, TransientFailure, MalformedResponse) as e:
            summary["errors"][sym] = str(e)
    return summary
