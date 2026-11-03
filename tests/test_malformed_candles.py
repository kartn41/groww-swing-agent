"""Groww returned candles with empty prices; one such candle used to fail the whole stock."""
import io, os, tempfile, unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import app.run as run
from app.data.groww_source import (GrowwCandleSource, MalformedResponse, parse_daily_candles, sync_daily)
from app.data.quality import validate_daily
from app.data.store import CandleStore
from tests.synthetic import business_days

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 26, 17, 0, tzinfo=IST)


def good(d, px=10.0):
    return [f"{d}T00:00:00", px, px + 1, px - 1, px + 0.5, 1000, None]


class TestParser(unittest.TestCase):
    def test_all_empty_candle_from_the_server_is_skipped_not_fatal(self):
        mal = []
        rows = parse_daily_candles({"candles": [good("2026-10-23"), ["2026-10-26T00:00:00"] + [None] * 6]}, mal)
        self.assertEqual([r[0] for r in rows], ["2026-10-23"])
        self.assertEqual(mal[0][0], "2026-10-26"); self.assertIn("missing or invalid price", mal[0][1])

    def test_each_kind_of_unusable_candle(self):
        bad = [["2026-10-01T00:00:00", None, 11, 9, 10, 5, None],        # one empty price
               ["2026-10-02T00:00:00", 10, 11, 9, 0, 5, None],           # zero price
               ["2026-10-05T00:00:00", 10, 11, 9, -3, 5, None],          # negative price
               ["2026-10-06T00:00:00", 10, float("nan"), 9, 10, 5, None],
               ["2026-10-07T00:00:00", "abc", 11, 9, 10, 5, None],       # not a number
               [None, 10, 11, 9, 10, 5, None],                           # no timestamp
               ["not a date", 10, 11, 9, 10, 5, None],
               ["2026-10-08T00:00:00", 10, 11],                          # too short
               "garbage"]
        mal = []
        rows = parse_daily_candles({"candles": bad + [good("2026-10-09")]}, mal)
        self.assertEqual([r[0] for r in rows], ["2026-10-09"]); self.assertEqual(len(mal), len(bad))

    def test_missing_volume_is_zero_and_prices_survive(self):
        rows = parse_daily_candles({"candles": [["2026-10-09T00:00:00", 10, 11, 9, 10.5, None, None],
                                                ["2026-10-12T00:00:00", 10, 11, 9, 10.5, -5, None]]})
        self.assertEqual([r[5] for r in rows], [0.0, 0.0]); self.assertEqual(rows[0][1:5], (10.0, 11.0, 9.0, 10.5))

    def test_clean_responses_unchanged_and_none_input_safe(self):
        self.assertEqual(len(parse_daily_candles({"candles": [good("2026-10-09")]})), 1)
        self.assertEqual(parse_daily_candles({}), []); self.assertEqual(parse_daily_candles(None), [])


class FakeG:
    EXCHANGE_NSE, SEGMENT_CASH, CANDLE_INTERVAL_DAY = "NSE", "CASH", "1d"

    def __init__(self, bad_dates=(), all_bad=False):
        self.bad_dates, self.all_bad, self.calls = set(bad_dates), all_bad, []

    def get_historical_candles(self, **kw):
        self.calls.append(kw["groww_symbol"])
        s, e = date.fromisoformat(kw["start_time"][:10]), date.fromisoformat(kw["end_time"][:10])
        out = []
        for d in business_days(date(2020, 1, 1), 2000):
            if s <= d <= e:
                out.append([f"{d}T00:00:00"] + [None] * 6 if (self.all_bad or d in self.bad_dates) else good(d))
        return {"candles": out}


def src(g):
    return GrowwCandleSource(g, sleep=lambda s: None)


class TestSyncWithBadCandles(unittest.TestCase):
    def test_stock_is_saved_with_the_good_candles_and_the_skip_is_reported(self):
        st = CandleStore(tempfile.mktemp())
        g = FakeG(bad_dates={date(2026, 10, 26), date(2026, 3, 4)})
        out = sync_daily(st, src(g), ["NSE-X"], NOW, history_start=date(2026, 1, 1))
        self.assertEqual(out["errors"], {}); self.assertIn("NSE-X", out["updated"])
        self.assertNotIn(date(2026, 3, 4), list(st.load("NSE-X").index))
        self.assertGreater(len(st.load("NSE-X")), 100)
        self.assertEqual(len(out["dropped"]["NSE-X"]), 2)
        self.assertIn("2026-03-04", [d for d, _ in out["dropped"]["NSE-X"]])

    def test_no_report_entry_for_clean_stocks(self):
        out = sync_daily(CandleStore(tempfile.mktemp()), src(FakeG()), ["NSE-X"], NOW, history_start=date(2026, 9, 1))
        self.assertEqual(out["dropped"], {})

    def test_skips_are_attributed_to_the_right_symbol(self):
        class PerSymbol(FakeG):
            def get_historical_candles(self, **kw):
                self.all_bad = kw["groww_symbol"] == "NSE-B"
                return super().get_historical_candles(**kw)
        out = sync_daily(CandleStore(tempfile.mktemp()), src(PerSymbol(bad_dates={date(2026, 9, 3)})),
                         ["NSE-A", "NSE-C"], NOW, history_start=date(2026, 9, 1))
        self.assertEqual(set(out["dropped"]), {"NSE-A", "NSE-C"})

    def test_a_response_that_is_mostly_unusable_is_an_error_not_silently_empty(self):
        st = CandleStore(tempfile.mktemp())
        out = sync_daily(st, src(FakeG(all_bad=True)), ["NSE-X"], NOW, history_start=date(2026, 9, 1))
        self.assertIn("MalformedResponse", out["errors"]["NSE-X"]); self.assertEqual(len(st.load("NSE-X")), 0)

    def test_format_change_on_every_stock_stops_the_run_after_three(self):
        st = CandleStore(tempfile.mktemp()); g = FakeG(all_bad=True)
        syms = [f"NSE-S{i}" for i in range(8)]
        out = sync_daily(st, src(g), syms, NOW, history_start=date(2026, 9, 1))
        self.assertIsNotNone(out["aborted"]); self.assertEqual(len(out["errors"]), 3)
        self.assertEqual(len(out["skipped"]), 5)

    def test_few_bad_candles_among_many_are_tolerated(self):
        g = FakeG(bad_dates={date(2026, 9, 2), date(2026, 9, 3)})              # 2 of ~40
        out = sync_daily(CandleStore(tempfile.mktemp()), src(g), ["NSE-X"], NOW, history_start=date(2026, 8, 1))
        self.assertEqual(out["errors"], {})


class TestSkippingDoesNotHideRealProblems(unittest.TestCase):
    def frame(self, n=260, drop=None):
        days = business_days(date(2025, 1, 1), n)
        df = pd.DataFrame([[10, 11, 9, 10.5, 1000]] * n, columns=["open", "high", "low", "close", "volume"], index=days)
        if drop:
            df = df.drop(index=drop)
        return df

    def test_a_missing_middle_day_is_still_usable(self):
        df = self.frame(); mid = df.index[100]
        self.assertTrue(validate_daily(df.drop(index=mid), df.index[-1]).ok)

    def test_a_missing_latest_day_blocks_the_stock_for_that_day(self):
        df = self.frame(); q = validate_daily(df.iloc[:-1], df.index[-1])
        self.assertFalse(q.ok); self.assertTrue(any("no candle for" in i for i in q.issues))


class TestFetchOutputAndProbe(unittest.TestCase):
    def test_probe_shows_raw_unusable_candles_and_saves_nothing(self):
        tmp = tempfile.mkdtemp(); db = f"{tmp}/market.db"
        g = FakeG(bad_dates={date(2026, 10, 26), date(2025, 6, 2)})
        buf = io.StringIO()
        with mock.patch.object(run, "MARKET_DB", db), \
             mock.patch("app.data.groww_source.groww_client_from_env", lambda: g), \
             mock.patch("app.data.groww_source.GrowwCandleSource",
                        lambda client, **kw: GrowwCandleSource(client, sleep=lambda s: None, **kw)), \
             redirect_stdout(buf):
            rc = run.main(["probe", "adanient", "--start", "2025-01-01"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("NSE-ADANIENT", out); self.assertIn("2 unusable", out)
        self.assertIn("UNUSABLE 2026-10-26", out); self.assertIn("missing or invalid price", out)
        self.assertFalse(os.path.exists(db), "probe must not write to the candle database")
        self.assertEqual(set(g.calls), {"NSE-ADANIENT"})


if __name__ == "__main__":
    unittest.main()
