#!/usr/bin/env python3
"""Fetch daily candles only through 2025-10-04 (safe range per scan). Writes to data/market.db."""
from __future__ import annotations
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config import DATA_DIR, PROJECT_ROOT  # noqa: E402
from app.data.groww_source import GrowwCandleSource, groww_client_from_env, drop_partial_today  # noqa: E402
from app.data.store import CandleStore  # noqa: E402
from app.env import load_env_file  # noqa: E402
from app.paper.engine import INDEX_SYMBOL, gsym  # noqa: E402
from app.universe import load_universe  # noqa: E402

GOOD_END = date(2025, 10, 4)
IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    load_env_file(str(PROJECT_ROOT / ".env"))
    now = datetime.now(IST)
    db = str(DATA_DIR / "market.db")
    store = CandleStore(db)
    pace = float(os.environ.get("GROWW_DATA_PACE", "1.5"))
    src = GrowwCandleSource(groww_client_from_env(), throttle_s=pace)

    uni, warns = load_universe(str(DATA_DIR / "universe" / "ind_nifty50list.csv"))
    for w in warns:
        print("warn:", w)
    symbols = sorted({INDEX_SYMBOL} | {gsym(c.symbol) for c in uni})

    con = sqlite3.connect(db)
    existing = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM candles")}
    con.close()

    print(f"Fetching through {GOOD_END} for {len(symbols)} symbols (pace {pace}s)...")
    for sym in symbols:
        last = store.last_date(sym)
        if last and last >= GOOD_END:
            print("skip", sym, "already", last)
            continue
        start = date(2020, 1, 1) if last is None else max(date(2020, 1, 1), last - timedelta(days=7))
        try:
            n = 0
            for rows in src.iter_daily(sym, start, GOOD_END):
                n += store.upsert(sym, drop_partial_today(rows, now))
            mx = store.last_date(sym)
            print("OK", sym, "upserted", n, "max", mx)
        except Exception as e:
            print("FAIL", sym, type(e).__name__, e)

    missing = set(symbols) - existing
    if missing:
        print("never had data before this run:", ", ".join(sorted(missing)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
