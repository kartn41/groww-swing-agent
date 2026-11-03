import tempfile
import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.intraday_groww import parse_intraday_candles
from app.data.intraday_store import IntradayStore
from app.intraday.engine import INDEX_SYMBOL, gsym, process_session
from app.intraday.orb import evaluate_orb, opening_range
from app.config import load_settings
from app.paper.ledger import Ledger
from app.universe import Constituent

IST = ZoneInfo("Asia/Kolkata")


def _bar_ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:00"


def _seed_orb_day(store: IntradayStore, day: date) -> None:
    """NIFTY flat; TEST stock breaks out at 10:00 with strong session RS."""
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    idx_rows, stk_rows = [], []
    for i in range(75):
        ts = t0 + timedelta(minutes=5 * i)
        if ts.hour > 15 or (ts.hour == 15 and ts.minute > 30):
            break
        label = ts.strftime("%Y-%m-%d %H:%M:%S")
        idx_c = 100.0
        if ts.time() <= datetime.strptime("09:45", "%H:%M").time():
            stk_h, stk_l, stk_c = 101.0, 100.0, 100.5
            vol = 1000.0
        elif ts.hour == 10 and ts.minute == 0:
            stk_h, stk_l, stk_c = 102.6, 102.0, 102.35
            vol = 5000.0
        else:
            stk_h, stk_l, stk_c = 102.5, 101.5, 102.0
            vol = 800.0
        idx_rows.append((label, idx_c, idx_c, idx_c, idx_c, 1e6))
        stk_rows.append((label, stk_c, stk_h, stk_l, stk_c, vol))
    store.upsert(INDEX_SYMBOL, idx_rows)
    store.upsert(gsym("TEST"), stk_rows)


class IntradayParseTests(unittest.TestCase):
    def test_parse_epoch(self):
        rows = parse_intraday_candles({"candles": [[1700000000, 10, 11, 9, 10.5, 100]]})
        self.assertEqual(len(rows), 1)
        self.assertIn(" ", rows[0][0])


class IntradayOrbTests(unittest.TestCase):
    def test_opening_range_and_breakout(self):
        day = date(2026, 6, 2)
        idx = pd.DataFrame(
            {"open": [100], "high": [100], "low": [100], "close": [100], "volume": [1e6]},
            index=pd.to_datetime([f"{day} 10:00:00"]),
        )
        bars = pd.DataFrame(
            {
                "open": [100, 100, 102.2],
                "high": [101, 102, 102.6],
                "low": [100, 100, 102],
                "close": [100.5, 101.5, 102.35],
                "volume": [1000, 1000, 5000],
            },
            index=pd.to_datetime([f"{day} 09:20:00", f"{day} 09:40:00", f"{day} 10:00:00"]),
        )
        orr = opening_range(bars, datetime.strptime("09:45", "%H:%M").time())
        self.assertTrue(orr.ready)
        out = evaluate_orb("TEST", "IT", bars, idx, orr)
        self.assertEqual(out.symbol, "TEST")
        self.assertGreater(out.entry, orr.high)


class IntradayEngineTests(unittest.TestCase):
    def test_session_fills_on_breakout(self):
        day = date(2026, 6, 2)
        with tempfile.TemporaryDirectory() as td:
            mkt = os.path.join(td, "mkt.db")
            paper = os.path.join(td, "paper.db")
            store = IntradayStore(mkt)
            _seed_orb_day(store, day)
            ledger = Ledger(paper, 250_000)
            uni = [Constituent("TEST", "IT")]
            res = process_session(day, ledger, store, uni, load_settings())
            self.assertFalse(res.skipped)
            self.assertTrue(res.fills or ledger.trades())
            if res.fills:
                self.assertIn("TEST", res.fills[0])


if __name__ == "__main__":
    unittest.main()
