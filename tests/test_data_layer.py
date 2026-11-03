import tempfile, unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pandas as pd
from app.data.store import CandleStore
from app.data.quality import validate_daily
from app.data.groww_source import (GrowwCandleSource, drop_partial_today, parse_daily_candles, sync_daily)
from tests.synthetic import business_days, make_series

IST = ZoneInfo("Asia/Kolkata")


class FakeG:
    EXCHANGE_NSE, SEGMENT_CASH, CANDLE_INTERVAL_DAY = "NSE", "CASH", "1d"

    def __init__(self, fail_for=()):
        self.calls, self.fail_for = [], set(fail_for)

    def get_historical_candles(self, **kw):
        self.calls.append(kw)
        if kw["groww_symbol"] in self.fail_for:
            raise RuntimeError("503")
        s, e = date.fromisoformat(kw["start_time"][:10]), date.fromisoformat(kw["end_time"][:10])
        days = [d for d in business_days(s, 400) if d <= e]
        return {"candles": [[f"{d}T00:00:00", 10, 11, 9, 10.5, 1000, None] for d in days]}


class TestStore(unittest.TestCase):
    def test_upsert_idempotent_and_end_filter(self):
        st = CandleStore(tempfile.mktemp())
        rows = [("2026-01-01", 1, 2, 0.5, 1.5, 10), ("2026-01-02", 1.5, 2, 1, 1.8, 12)]
        st.upsert("X", rows); st.upsert("X", rows)
        self.assertEqual(len(st.load("X")), 2)
        self.assertEqual(len(st.load("X", end=date(2026, 1, 1))), 1)
        self.assertEqual(st.last_date("X"), date(2026, 1, 2))
        self.assertIsNone(st.last_date("Y"))


class TestParsing(unittest.TestCase):
    def test_formats(self):
        rows = parse_daily_candles({"candles": [
            ["2025-09-24T00:00:00", 1, 2, 0.5, 1.5, 100, None],
            ["2025-09-25 00:00:00", 1, 2, 0.5, 1.5, None, None],
            [1758844800, 1, 2, 0.5, 1.5, 7, None]]})
        self.assertEqual([r[0] for r in rows], ["2025-09-24", "2025-09-25", "2025-09-26"])
        self.assertEqual(rows[1][5], 0.0)
        self.assertEqual(parse_daily_candles({}), [])


class TestSource(unittest.TestCase):
    def test_chunks_under_180_days(self):
        g = FakeG()
        GrowwCandleSource(g, sleep=lambda s: None).daily("NSE-X", date(2024, 1, 1), date(2025, 2, 1))
        self.assertGreaterEqual(len(g.calls), 3)
        for c in g.calls:
            span = date.fromisoformat(c["end_time"][:10]) - date.fromisoformat(c["start_time"][:10])
            self.assertLessEqual(span.days, 180)
            self.assertEqual(c["segment"], "CASH")

    def test_missing_interval_constant_fails_loudly(self):
        class NoInterval: pass
        with self.assertRaises(RuntimeError):
            GrowwCandleSource(NoInterval())

    def test_partial_candle_dropped_before_close(self):
        rows = [("2026-10-23", 1, 1, 1, 1, 1), ("2026-10-26", 1, 1, 1, 1, 1)]
        before = datetime(2026, 10, 26, 11, 0, tzinfo=IST)
        after = datetime(2026, 10, 26, 16, 0, tzinfo=IST)
        self.assertEqual(len(drop_partial_today(rows, before)), 1)
        self.assertEqual(len(drop_partial_today(rows, after)), 2)

    def test_sync_incremental_and_isolates_errors(self):
        st, g = CandleStore(tempfile.mktemp()), FakeG(fail_for={"NSE-BAD"})
        st.upsert("NSE-A", [("2026-10-01", 1, 2, 0.5, 1.5, 1)])
        src = GrowwCandleSource(g, sleep=lambda s: None)
        out = sync_daily(st, src, ["NSE-A", "NSE-BAD", "NSE-B"], datetime(2026, 10, 23, 17, 0, tzinfo=IST))
        self.assertIn("NSE-BAD", out["errors"]); self.assertIn("NSE-B", out["updated"])
        a_call = next(c for c in g.calls if c["groww_symbol"] == "NSE-A")
        self.assertEqual(a_call["start_time"][:10], "2026-09-24")       # last - 7 days
        b_call = next(c for c in g.calls if c["groww_symbol"] == "NSE-B")
        self.assertEqual(b_call["start_time"][:10], "2020-01-01")       # full history


class TestQuality(unittest.TestCase):
    def frame(self, n=260):
        days = business_days(date(2025, 1, 1), n)
        rows = make_series(days, 1, 0.0005, 0.01, 100)
        df = pd.DataFrame(rows, columns=["d", "open", "high", "low", "close", "volume"])
        df["d"] = pd.to_datetime(df["d"]).dt.date
        return df.set_index("d")

    def test_clean_data_ok(self):
        df = self.frame()
        self.assertTrue(validate_daily(df, df.index[-1]).ok)

    def test_stale_short_inconsistent_and_split_are_blocking(self):
        df = self.frame()
        self.assertFalse(validate_daily(df.iloc[:-1], df.index[-1]).ok)       # no candle for as_of
        self.assertFalse(validate_daily(df.iloc[-100:], df.index[-1]).ok)     # too short
        bad = df.copy(); bad.iloc[-3, bad.columns.get_loc("high")] = 1.0
        self.assertFalse(validate_daily(bad, df.index[-1]).ok)
        split = df.copy(); split.iloc[-10:, :4] = split.iloc[-10:, :4] / 2   # unadjusted 1:2 split
        q = validate_daily(split, df.index[-1])
        self.assertFalse(q.ok); self.assertTrue(any("split" in i for i in q.issues))


if __name__ == "__main__":
    unittest.main()
