"""Read-only Groww data access (historical candles). Facts from Groww docs (2026-10-24):
 get_historical_candles(exchange, segment, groww_symbol='NSE-INFY' / index 'NSE-NIFTY',
 start_time/end_time 'yyyy-MM-dd HH:mm:ss', candle_interval), max 180 days per daily request,
 data from 2020. Response: {'candles': [[ts, o, h, l, c, volume, oi], ...]}.
Data APIs do not need the static IP."""
from __future__ import annotations
import logging
import math
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
CHUNK_DAYS = 175                        # under the 180-day limit
DAILY_INTERVAL_NAMES = ("CANDLE_INTERVAL_DAY", "CANDLE_INTERVAL_1_DAY", "CANDLE_INTERVAL_DAILY")
FINAL_CANDLE_AFTER = dtime(15, 45)      # today's daily candle is partial before this


def groww_access_token_from_env(environ=None) -> str:
    """TOTP login -> today's access token (valid until 06:00 next day)."""
    import pyotp
    from growwapi import GrowwAPI
    env = os.environ if environ is None else environ
    tok, sec = env.get("GROWW_TOTP_TOKEN"), env.get("GROWW_TOTP_SECRET")
    if not tok or not sec:
        raise RuntimeError("GROWW_TOTP_TOKEN / GROWW_TOTP_SECRET missing in .env")
    return GrowwAPI.get_access_token(api_key=tok, totp=pyotp.TOTP(sec).now())


def groww_client_from_env(environ=None):
    """GrowwAPI client. Used for DATA only in paper mode."""
    from growwapi import GrowwAPI
    return GrowwAPI(groww_access_token_from_env(environ))


def _num(x):
    """float or None; never raises. NaN and infinities count as missing."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_daily_candles(resp: dict, skipped: list | None = None) -> list[tuple]:
    """Keep every usable candle; skip unusable ones instead of failing the whole stock. Nothing is repaired
    or guessed. `skipped` receives (date, reason) for each one. Groww's docs do not mention empty prices,
    but real responses contain them (observed: a candle with None open/high/low/close)."""
    rows = []
    for c in (resp or {}).get("candles") or []:
        label = "?"
        try:
            ts = c[0]
            if isinstance(ts, (int, float)):
                d = datetime.fromtimestamp(ts, IST).date()
            else:
                d = date.fromisoformat(str(ts).replace("T", " ")[:10])
            label = d.isoformat()
        except Exception:
            if skipped is not None:
                skipped.append((label, f"unreadable timestamp or row {str(c)[:60]}"))
            continue
        vals = [_num(x) for x in c[1:5]]
        if len(vals) < 4 or None in vals or min(vals) <= 0:
            if skipped is not None:
                skipped.append((label, f"missing or invalid price {list(c[1:5])}"))
            continue
        v = _num(c[5]) if len(c) > 5 else 0.0
        rows.append((d.isoformat(), vals[0], vals[1], vals[2], vals[3], max(v, 0.0) if v is not None else 0.0))
    return rows


class FatalDataError(Exception):
    """Authentication/authorisation failure: every further request would fail too. Stop the whole run."""


class RateLimited(Exception):
    """Still rate limited after all backoff attempts for one request."""


class MalformedResponse(Exception):
    """Most candles in a response were unusable: the response format may have changed."""


class TransientFailure(Exception):
    """Network/timeout problem that persisted through all retries."""


def classify(e: Exception) -> str:
    """'rate' | 'auth' | 'transient' | 'other'. Uses the exception class AND its message, because Groww
    raised plain GrowwAPIException (not GrowwAPIRateLimitException) for a rate-limit breach in practice."""
    name, msg = type(e).__name__.lower(), str(e).lower()
    code = str(getattr(e, "code", "") or "").lower()
    if "ratelimit" in name or "rate limit" in msg or "too many requests" in msg or code in ("429", "ga003"):
        return "rate"
    if ("authentication" in name or "authorisation" in name or "authorization" in name or code in ("401", "403")
            or any(k in msg for k in ("unauthor", "forbidden", "invalid token", "token expired", "authentication failed"))):
        return "auth"
    if "timeout" in name or "timed out" in msg or isinstance(e, (TimeoutError, ConnectionError)):
        return "transient"
    return "other"


class GrowwCandleSource:
    """Paced, retrying access to Groww historical candles. The documented rate-limit table does not list
    this endpoint, so the limit is unknown: pace conservatively, back off on any breach, slow down for the
    rest of the run after one, and give up (rather than hammer) if it persists."""

    def __init__(self, groww, throttle_s: float = 0.6, sleep=time.sleep, monotonic=time.monotonic,
                 rate_waits=(30, 60, 90, 120), transient_waits=(2, 5, 10), max_pace_s: float = 3.0) -> None:
        self.g, self.pace_s, self.sleep, self._now = groww, throttle_s, sleep, monotonic
        self.rate_waits, self.transient_waits, self.max_pace_s = tuple(rate_waits), tuple(transient_waits), max_pace_s
        self.stats = {"requests": 0, "rate_limited": 0, "transient": 0}
        self._skipped: list[tuple] = []
        self._last: float | None = None
        self._skipped: list = []
        self.interval = next((getattr(groww, n) for n in DAILY_INTERVAL_NAMES if hasattr(groww, n)), None)
        if self.interval is None:
            raise RuntimeError(f"growwapi has none of {DAILY_INTERVAL_NAMES}; check the Annexures page")

    def _wait_turn(self) -> None:
        """Keep at least pace_s between request starts. Applied BEFORE every request, including retries."""
        if self._last is not None:
            gap = self.pace_s - (self._now() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self._now()

    def _call(self, **kw):
        rate_i = trans_i = 0
        while True:
            self._wait_turn()
            self.stats["requests"] += 1
            try:
                return self.g.get_historical_candles(**kw)
            except Exception as e:
                kind = classify(e)
                if kind == "auth":
                    raise FatalDataError(f"{type(e).__name__}: {e}") from e
                if kind == "rate":
                    self.stats["rate_limited"] += 1
                    if rate_i >= len(self.rate_waits):
                        raise RateLimited(f"{type(e).__name__}: {e}") from e
                    wait, rate_i = self.rate_waits[rate_i], rate_i + 1
                    self.pace_s = min(self.pace_s * 1.5, self.max_pace_s)          # slower for the rest of the run
                    log.warning("rate limited; waiting %ss then retrying (pace now %.2fs)", wait, self.pace_s)
                    self.sleep(wait)
                    self._last = self._now()
                    continue
                if kind == "transient":
                    self.stats["transient"] += 1
                    if trans_i >= len(self.transient_waits):
                        raise TransientFailure(f"{type(e).__name__}: {e}") from e
                    wait, trans_i = self.transient_waits[trans_i], trans_i + 1
                    log.warning("network problem (%s); retrying in %ss", type(e).__name__, wait)
                    self.sleep(wait)
                    continue
                raise                                   # symbol-specific problem: caller records it

    def iter_raw(self, groww_symbol: str, start: date, end: date):
        """Yield (chunk_start, chunk_end, raw response) for each 175-day chunk."""
        s = start
        while s <= end:
            e = min(s + timedelta(days=CHUNK_DAYS), end)
            resp = self._call(
                exchange=self.g.EXCHANGE_NSE, segment=self.g.SEGMENT_CASH, groww_symbol=groww_symbol,
                start_time=f"{s} 09:00:00", end_time=f"{e} 15:59:59", candle_interval=self.interval)
            yield s, e, resp
            s = e + timedelta(days=1)

    def iter_daily(self, groww_symbol: str, start: date, end: date):
        """Yield the usable rows of each chunk as soon as it arrives, so callers can save progress."""
        for s, e, resp in self.iter_raw(groww_symbol, start, end):
            before = len(self._skipped)
            rows = parse_daily_candles(resp, self._skipped)
            bad, total = len(self._skipped) - before, len((resp or {}).get("candles") or [])
            if total >= 5 and bad > total * 0.5:
                raise MalformedResponse(f"{bad} of {total} candles unusable for {groww_symbol} {s}..{e}")
            yield rows

    def take_skipped(self) -> list:
        """Hand over (and forget) the unusable candles seen since the last call."""
        out, self._skipped = self._skipped, []
        return out

    def daily(self, groww_symbol: str, start: date, end: date) -> list[tuple]:
        rows: list[tuple] = []
        for chunk in self.iter_daily(groww_symbol, start, end):
            rows.extend(chunk)
        return rows


def drop_partial_today(rows: list[tuple], now: datetime) -> list[tuple]:
    now = now.astimezone(IST)
    if now.time() >= FINAL_CANDLE_AFTER:
        return rows
    return [r for r in rows if r[0] != now.date().isoformat()]


def sync_daily(store, source, groww_symbols: list[str], now: datetime,
               history_start: date = date(2020, 1, 1), overlap_days: int = 7,
               max_consecutive_failures: int = 3, end_caps: dict[str, date] | None = None) -> dict:
    """Incremental: refetch the last week (catches late corrections), else full history.
    Each 175-day chunk is saved as it arrives, so an interrupted run resumes where it stopped.
    end_caps: symbol -> last date to ask Groww for; a capped symbol already stored past its cap is not requested."""
    summary = {"updated": {}, "errors": {}, "aborted": None, "skipped": [], "dropped": {}, "capped": []}
    now_ist = now.astimezone(IST)
    # Today's daily candle is still forming until 15:45 IST (and may carry empty values), and would be
    # discarded anyway: do not ask for it.
    today = now_ist.date() if now_ist.time() >= FINAL_CANDLE_AFTER else now_ist.date() - timedelta(days=1)
    systemic = 0
    for i, sym in enumerate(groww_symbols):
        try:
            last = store.last_date(sym)
            start = (last - timedelta(days=overlap_days)) if last else history_start
            end = min(today, end_caps[sym]) if end_caps and sym in end_caps else today
            if start > end:
                summary["capped"].append(sym)
                continue
            n = 0
            for rows in source.iter_daily(sym, start, end):
                n += store.upsert(sym, drop_partial_today(rows, now))
            summary["updated"][sym] = n
            systemic = 0
            dropped = source.take_skipped() if hasattr(source, "take_skipped") else []
            if dropped:
                summary["dropped"][sym] = dropped
                log.warning("%s: skipped %d invalid candle(s), e.g. %s", sym, len(dropped), dropped[0])
            log.info("fetched %s: %d rows (%d/%d)", sym, n, i + 1, len(groww_symbols))
        except FatalDataError as e:
            summary["errors"][sym] = f"{type(e).__name__}: {e}"
            summary["aborted"] = f"login/permission problem, stopped immediately: {e}"
            summary["skipped"] = list(groww_symbols[i + 1:])
            log.error("fetch aborted: %s", e)
            break
        except Exception as e:                      # one bad symbol must not stop the rest
            summary["errors"][sym] = f"{type(e).__name__}: {e}"
            log.warning("fetch failed for %s: %s", sym, e)
            if hasattr(source, "take_skipped"):
                source.take_skipped()
            systemic = systemic + 1 if isinstance(e, (RateLimited, TransientFailure, MalformedResponse)) else 0
            if systemic >= max_consecutive_failures:
                summary["aborted"] = (f"{systemic} stocks in a row failed even after waiting ({type(e).__name__}); "
                                      "stopped so Groww is not hammered. Progress is saved: run it again later.")
                summary["skipped"] = list(groww_symbols[i + 1:])
                log.error("fetch aborted: %s", summary["aborted"])
                break
    stats = getattr(source, "stats", None)
    if stats:
        summary["stats"] = dict(stats, final_pace_s=round(getattr(source, "pace_s", 0), 2))
    return summary
