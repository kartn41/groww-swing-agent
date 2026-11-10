"""Read-only live market data for the ETFs (Groww live-data + historical candle APIs). Used in PAPER and LIVE.
Response field names are read defensively: anything missing or non-positive => MarketDataError (fail closed)."""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
INTERVALS = {"1h": ("CANDLE_INTERVAL_HOUR_1", 60), "15m": ("CANDLE_INTERVAL_MIN_15", 15)}


class MarketDataError(Exception):
    pass


@dataclass
class Quote:
    symbol: str
    ltp: float
    bid: Optional[float]
    ask: Optional[float]
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None

    @property
    def spread_bps(self) -> Optional[float]:
        if not self.bid or not self.ask or self.ask < self.bid:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 1e4


def _pos(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def parse_quote(symbol: str, r: dict) -> Quote:
    if not isinstance(r, dict):
        raise MarketDataError(f"{symbol}: unexpected quote payload")
    ltp = _pos(r.get("last_price"))
    if ltp is None:
        raise MarketDataError(f"{symbol}: quote has no usable last_price")
    depth = r.get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    bid = _pos(buy[0].get("price")) if buy and isinstance(buy[0], dict) else None
    ask = _pos(sell[0].get("price")) if sell and isinstance(sell[0], dict) else None
    bid = bid or _pos(r.get("bid_price"))
    ask = ask or _pos(r.get("offer_price"))
    ohlc = r.get("ohlc") if isinstance(r.get("ohlc"), dict) else {}
    return Quote(symbol, ltp, bid, ask, _pos(ohlc.get("open")), _pos(ohlc.get("high")), _pos(ohlc.get("low")))


def parse_candles(resp: dict, minutes: int, now: datetime) -> pd.DataFrame:
    """Completed candles only: a candle starting at s is complete once s + interval <= now."""
    rows = []
    for c in (resp or {}).get("candles") or []:
        try:
            ts = c[0]
            dt = datetime.fromtimestamp(ts, IST) if isinstance(ts, (int, float)) else \
                datetime.fromisoformat(str(ts).replace("T", " ")[:19]).replace(tzinfo=IST)
            o, h, l, cl = (_pos(x) for x in c[1:5])
        except Exception:
            continue
        if None in (o, h, l, cl):
            continue
        if dt + timedelta(minutes=minutes) > now:
            continue
        v = c[5] if len(c) > 5 and c[5] is not None else 0.0
        rows.append((dt, o, h, l, cl, float(v)))
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates("dt").set_index("dt").sort_index()


class GrowwMarket:
    def __init__(self, groww, now=lambda: datetime.now(IST)) -> None:
        self.g, self._now = groww, now

    def quote(self, symbol: str) -> Quote:
        g = self.g
        try:
            r = g.get_quote(trading_symbol=symbol, exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, timeout=5)
        except Exception as e:
            raise MarketDataError(f"{symbol}: quote failed ({type(e).__name__})") from e
        return parse_quote(symbol, r)

    def candles(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        g = self.g
        name, minutes = INTERVALS[interval]
        now = self._now()
        start = now - timedelta(days=days)
        try:
            resp = g.get_historical_candles(
                exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, groww_symbol=f"NSE-{symbol}",
                start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=getattr(g, name))
        except Exception as e:
            raise MarketDataError(f"{symbol}: {interval} candles failed ({type(e).__name__})") from e
        return parse_candles(resp, minutes, now)
