"""Earnings / corporate-event calendar.

Source: a CSV you download from NSE's corporate event calendar (or maintain yourself) and save as
data/events/events.csv. Automatic scraping of NSE is deliberately not done. Columns are matched
flexibly: a symbol column, a date column, and an optional purpose/subject column.

Policy:
 - Events whose purpose looks price-moving (results, bonus, split, rights, buyback, merger...) block
   NEW entries in that stock within `block_days` calendar days.
 - Other events (dividend, AGM...) are reported but do not block.
 - Open positions are never force-sold because of an event; the report/Telegram warns you instead.
 - Missing or stale calendar: a warning in PAPER mode; blocks new entries when events are required.
"""
from __future__ import annotations
import csv
import os
import time
from dataclasses import dataclass
from datetime import date, datetime

BLOCKING_KEYWORDS = ("result", "financial", "bonus", "split", "sub-division", "subdivision", "rights",
                     "buyback", "buy back", "merger", "amalgamation", "demerger", "scheme", "delist")
SYMBOL_COLS = ("symbol",)
DATE_COLS = ("date", "event date", "bm_date", "board meeting date", "ex-date", "ex date", "record date")
PURPOSE_COLS = ("purpose", "subject", "event", "type", "details")
DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%B-%Y")


class EventsError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    symbol: str
    d: date
    purpose: str

    @property
    def blocking(self) -> bool:
        p = self.purpose.lower()
        return any(k in p for k in BLOCKING_KEYWORDS) or not p.strip()   # unknown purpose => cautious


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(s)


class EventCalendar:
    def __init__(self, events: list[Event], warnings: list[str] | None = None, usable: bool = True):
        self.by_symbol: dict[str, list[Event]] = {}
        for e in events:
            self.by_symbol.setdefault(e.symbol, []).append(e)
        self.warnings, self.usable = warnings or [], usable

    def upcoming(self, symbol: str, start: date, days: int) -> list[Event]:
        return sorted([e for e in self.by_symbol.get(symbol, []) if 0 <= (e.d - start).days <= days],
                      key=lambda e: e.d)

    def blocking_event(self, symbol: str, start: date, days: int) -> Event | None:
        return next((e for e in self.upcoming(symbol, start, days) if e.blocking), None)


def load_events(path: str, stale_days: int = 14, now: float | None = None) -> EventCalendar:
    if not os.path.exists(path):
        return EventCalendar([], [f"event calendar missing ({path}); upcoming results are NOT being checked"],
                             usable=False)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        pick = lambda names: next((cols[n] for n in names if n in cols), None)
        sc, dc, pc = pick(SYMBOL_COLS), pick(DATE_COLS), pick(PURPOSE_COLS)
        if not sc or not dc:
            raise EventsError(f"event CSV needs a symbol and a date column; found {reader.fieldnames}")
        events, bad = [], 0
        for row in reader:
            sym = (row.get(sc) or "").strip().upper()
            try:
                d = _parse_date(row.get(dc) or "")
            except ValueError:
                bad += 1
                continue
            if sym:
                events.append(Event(sym, d, (row.get(pc) or "").strip() if pc else ""))
    warnings = []
    if bad:
        warnings.append(f"{bad} event rows had unreadable dates and were skipped")
    age = ((now or time.time()) - os.path.getmtime(path)) / 86400
    usable = True
    if age > stale_days:
        warnings.append(f"event calendar is {age:.0f} days old; refresh it from NSE's event calendar")
        usable = False
    return EventCalendar(events, warnings, usable)
