"""Stock daily candles from NSE's official end-of-day file ("bhavcopy", UDiFF format, published after the close).
One zip per trading day holds every NSE stock: TradDt, TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric,
TtlTradgVol, ... Only series EQ is used. Prices are as traded (not adjusted for later splits or bonuses).
Read-only, no login. Standard library only."""
from __future__ import annotations
import csv
import io
import logging
import math
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from typing import Callable

log = logging.getLogger(__name__)
URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
           "Accept": "*/*"}
REQUIRED = ("TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol")


class MalformedBhavcopy(Exception):
    """The file is not the expected UDiFF bhavcopy (format change or a wrong file)."""


class BhavcopyBlocked(Exception):
    """NSE refused the download (HTTP 401/403). Retrying will not help."""


class BhavcopyUnavailable(Exception):
    """Network or server error that persisted through all retries."""


def _num(x):
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_bhavcopy(blob: bytes, expected_day: date, skipped: list | None = None) -> dict[str, tuple]:
    """symbol -> (date, open, high, low, close, volume) for every usable EQ row."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise MalformedBhavcopy(f"expected one CSV in the zip, found {names}")
        text = zf.read(names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile as e:
        raise MalformedBhavcopy(f"not a zip file: {e}") from e
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise MalformedBhavcopy(f"columns missing: {missing}")
    out: dict[str, tuple] = {}
    for row in reader:
        if (row["SctySrs"] or "").strip().upper() != "EQ":
            continue
        sym = (row["TckrSymb"] or "").strip().upper()
        try:
            d = date.fromisoformat((row["TradDt"] or "").strip())
        except ValueError:
            raise MalformedBhavcopy(f"unreadable trade date {row['TradDt']!r} for {sym}")
        if d != expected_day:
            raise MalformedBhavcopy(f"file for {expected_day} contains trade date {d}")
        o, h, l, c = (_num(row[k]) for k in ("OpnPric", "HghPric", "LwPric", "ClsPric"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            if skipped is not None:
                skipped.append((sym, f"missing or invalid price {[row[k] for k in ('OpnPric', 'HghPric', 'LwPric', 'ClsPric')]}"))
            continue
        v = _num(row["TtlTradgVol"])
        out[sym] = (d.isoformat(), o, h, l, c, max(v, 0.0) if v is not None else 0.0)
    return out


class BhavcopySource:
    def __init__(self, opener: Callable = urllib.request.urlopen, sleep=time.sleep, monotonic=time.monotonic,
                 pace_s: float = 1.0, timeout: float = 30.0, waits=(5, 15, 30)) -> None:
        self._open, self.sleep, self._now = opener, sleep, monotonic
        self.pace_s, self.timeout, self.waits = pace_s, timeout, tuple(waits)
        self._last: float | None = None
        self.requests = 0

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self.pace_s - (self._now() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self._now()

    def get(self, d: date) -> bytes | None:
        """The zip for day d, or None if NSE has no file for it (not published yet, or no session)."""
        req = urllib.request.Request(URL.format(ymd=d.strftime("%Y%m%d")), headers=HEADERS)
        attempt = 0
        while True:
            self._wait_turn()
            self.requests += 1
            try:
                with self._open(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise BhavcopyBlocked(f"NSE refused the download for {d} (HTTP {e.code})") from e
                why = f"HTTP {e.code}"
            except Exception as e:                       # timeouts, resets, DNS
                why = type(e).__name__
            if attempt >= len(self.waits):
                raise BhavcopyUnavailable(f"bhavcopy for {d}: {why} after {attempt + 1} attempts")
            log.warning("bhavcopy %s: %s; retrying in %ss", d, why, self.waits[attempt])
            self.sleep(self.waits[attempt])
            attempt += 1


def sync_bhavcopy(store, source, groww_symbols: list[str], trading_days: list[date], joinable_after: date) -> dict:
    """Add each stock's candle for every trading day after its last stored one. A symbol whose stored history
    stops before `joinable_after` is left alone, so a gap is never papered over. Stops at the first day NSE has
    no file for: the latest day is then simply not published yet; an older day is reported as an error."""
    summary = {"days": [], "rows": 0, "absent": {}, "behind": [], "dropped": {}, "errors": {},
               "aborted": None, "not_published": None}
    last = {s: store.last_date(s) for s in groww_symbols}
    eligible = [s for s in groww_symbols if last[s] is None or last[s] >= joinable_after]
    summary["behind"] = sorted(set(groww_symbols) - set(eligible))
    for d in trading_days:
        need = [s for s in eligible if last[s] is None or last[s] < d]
        if not need:
            continue
        try:
            blob = source.get(d)
            if blob is None:
                if d == trading_days[-1]:
                    summary["not_published"] = d.isoformat()
                else:
                    summary["errors"][f"bhavcopy {d}"] = "no file on NSE for this trading day (HTTP 404)"
                    summary["aborted"] = f"NSE bhavcopy for {d} is missing; later days were not loaded"
                break
            skipped: list = []
            rows = parse_bhavcopy(blob, d, skipped)
        except (BhavcopyBlocked, BhavcopyUnavailable, MalformedBhavcopy) as e:
            summary["errors"][f"bhavcopy {d}"] = f"{type(e).__name__}: {e}"
            summary["aborted"] = f"NSE bhavcopy download stopped at {d}: {e}"
            log.error("bhavcopy sync stopped: %s", e)
            break
        for s in need:
            r = rows.get(s.removeprefix("NSE-"))
            if r is None:
                summary["absent"].setdefault(s, []).append(d.isoformat())
                continue
            store.upsert(s, [r])
            last[s] = d
            summary["rows"] += 1
        for sym, why in skipped:
            if f"NSE-{sym}" in need:
                summary["dropped"].setdefault(f"NSE-{sym}", []).append((d.isoformat(), why))
        summary["days"].append(d.isoformat())
    summary["requests"] = getattr(source, "requests", 0)
    return summary
