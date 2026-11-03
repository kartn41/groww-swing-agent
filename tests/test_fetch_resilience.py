"""Fetch behaviour under rate limits and failures (review of the first real server run)."""
import fcntl, os, tempfile, unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import app.run as run
from app.data.groww_source import (FatalDataError, GrowwCandleSource, RateLimited, TransientFailure, classify,
                                   sync_daily)
from app.data.store import CandleStore
from tests.synthetic import business_days

IST = ZoneInfo("Asia/Kolkata")
RATE_MSG = "Rate limit has breached for your request. Please try again later."
NOW = datetime(2026, 10, 26, 17, 0, tzinfo=IST)


class GrowwAPIException(Exception):                 # the plain base class, as seen in the real log
    def __init__(self, msg, code=None):
        super().__init__(msg); self.code = code


class GrowwAPIRateLimitException(GrowwAPIException): pass
class GrowwAPIAuthenticationException(GrowwAPIException): pass
class GrowwAPIBadRequestException(GrowwAPIException): pass


class Clock:
    """Fake time: sleeping advances it, so pacing can be asserted without waiting."""
    def __init__(self): self.t, self.sleeps = 1000.0, []
    def now(self): return self.t
    def sleep(self, s): self.sleeps.append(s); self.t += s


class FakeG:
    EXCHANGE_NSE, SEGMENT_CASH, CANDLE_INTERVAL_DAY = "NSE", "CASH", "1d"

    def __init__(self, clock, script=None, per_symbol=None):
        self.clock, self.script, self.per_symbol = clock, list(script or []), per_symbol or {}
        self.calls = []                                # (time, symbol, start)
        self.ends = []                                 # requested end dates
        self.empty_dates: set[str] = set()             # dates whose candle comes back with None prices (all symbols)
        self.empty_for: dict[str, set[str]] = {}       # ... or only for one symbol
        self.fail_second_call_for: set[str] = set()    # symbols whose 2nd request raises a symbol-specific error

    def get_historical_candles(self, **kw):
        sym = kw["groww_symbol"]
        self.calls.append((self.clock.now(), sym, kw["start_time"][:10]))
        self.ends.append(kw["end_time"][:10])
        if sym in self.fail_second_call_for and sum(1 for _, x, _ in self.calls if x == sym) == 2:
            raise GrowwAPIBadRequestException("no such symbol")
        item = self.script.pop(0) if self.script else self.per_symbol.get(sym)
        if isinstance(item, Exception):
            raise item
        s, e = date.fromisoformat(kw["start_time"][:10]), date.fromisoformat(kw["end_time"][:10])
        days = [d for d in business_days(date(2020, 1, 1), 2000) if s <= d <= e]
        def candle(d):
            empty = str(d) in self.empty_dates or str(d) in self.empty_for.get(sym, ())
            prices = [None] * 4 if empty else [10, 11, 9, 10.5]
            return [f"{d}T00:00:00", *prices, 1000, None]
        return {"candles": [candle(d) for d in days]}


def source(g, clock, **kw):
    return GrowwCandleSource(g, sleep=clock.sleep, monotonic=clock.now, **kw)


class TestClassify(unittest.TestCase):
    def test_plain_base_exception_with_rate_limit_message_is_a_rate_limit(self):
        self.assertEqual(classify(GrowwAPIException(RATE_MSG)), "rate")      # exactly what the server log showed

    def test_other_rate_limit_shapes(self):
        self.assertEqual(classify(GrowwAPIRateLimitException("slow down")), "rate")
        self.assertEqual(classify(GrowwAPIException("x", code="429")), "rate")
        self.assertEqual(classify(Exception("Too Many Requests")), "rate")

    def test_auth_errors(self):
        self.assertEqual(classify(GrowwAPIAuthenticationException("bad")), "auth")
        self.assertEqual(classify(GrowwAPIException("Unauthorized access")), "auth")
        self.assertEqual(classify(GrowwAPIException("x", code="403")), "auth")

    def test_transient_and_other(self):
        self.assertEqual(classify(TimeoutError("t")), "transient")
        self.assertEqual(classify(ConnectionResetError("r")), "transient")
        self.assertEqual(classify(GrowwAPIBadRequestException("no such symbol")), "other")
        self.assertEqual(classify(ValueError("bug")), "other")

    def test_rate_limit_wins_over_auth_words(self):
        self.assertEqual(classify(GrowwAPIException("Rate limit hit on token endpoint")), "rate")


class TestBackoff(unittest.TestCase):
    def test_one_rate_limit_then_success_waits_and_slows_down(self):
        c = Clock(); g = FakeG(c, script=[GrowwAPIException(RATE_MSG)])
        src = source(g, c, throttle_s=0.6)
        rows = src.daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertTrue(rows); self.assertEqual(len(g.calls), 2)
        self.assertIn(30, c.sleeps); self.assertGreater(src.pace_s, 0.6)
        self.assertEqual(src.stats["rate_limited"], 1)

    def test_persistent_rate_limit_gives_up_after_bounded_retries(self):
        c = Clock(); g = FakeG(c, per_symbol={"NSE-X": GrowwAPIException(RATE_MSG)})
        src = source(g, c)
        with self.assertRaises(RateLimited):
            src.daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(len(g.calls), 1 + 4)                          # first try + one per wait, never more
        self.assertEqual([x for x in c.sleeps if x >= 30], [30, 60, 90, 120])

    def test_pace_never_exceeds_cap(self):
        c = Clock(); g = FakeG(c, per_symbol={"NSE-X": GrowwAPIException(RATE_MSG)})
        src = source(g, c, max_pace_s=1.0)
        with self.assertRaises(RateLimited):
            src.daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertLessEqual(src.pace_s, 1.0)

    def test_network_blips_retry_then_succeed_or_give_up(self):
        c = Clock(); g = FakeG(c, script=[TimeoutError("t"), ConnectionResetError("r")])
        self.assertTrue(source(g, c).daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1)))
        c2 = Clock(); g2 = FakeG(c2, per_symbol={"NSE-X": TimeoutError("t")})
        with self.assertRaises(TransientFailure):
            source(g2, c2).daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(len(g2.calls), 4)

    def test_auth_failure_is_fatal_and_not_retried(self):
        c = Clock(); g = FakeG(c, per_symbol={"NSE-X": GrowwAPIAuthenticationException("bad token")})
        with self.assertRaises(FatalDataError):
            source(g, c).daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(len(g.calls), 1)

    def test_symbol_specific_error_is_not_retried(self):
        c = Clock(); g = FakeG(c, per_symbol={"NSE-X": GrowwAPIBadRequestException("no such symbol")})
        with self.assertRaises(GrowwAPIBadRequestException):
            source(g, c).daily("NSE-X", date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(len(g.calls), 1)


class TestPacing(unittest.TestCase):
    def test_requests_are_spaced_before_every_call_including_the_first_after_a_failure(self):
        c = Clock(); g = FakeG(c, script=[GrowwAPIException(RATE_MSG)])
        src = source(g, c, throttle_s=0.6)
        src.daily("NSE-X", date(2024, 1, 1), date(2026, 10, 1))        # several chunks
        times = [t for t, _, _ in g.calls]
        self.assertGreater(len(times), 4)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 0.6 - 1e-9)

    def test_pace_is_configurable_through_the_environment_name(self):
        self.assertEqual(GrowwCandleSource(FakeG(Clock()), throttle_s=1.5).pace_s, 1.5)


class TestChunkLevelSaving(unittest.TestCase):
    def test_progress_survives_a_failure_part_way_through_a_stock_and_resumes(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        history_start = date(2024, 1, 1)                       # about 5 chunks of 175 days
        g = FakeG(c)
        good = g.get_historical_candles
        n = {"calls": 0}

        def first_two_chunks_then_rate_limited(**kw):
            n["calls"] += 1
            if n["calls"] >= 3:                                # 3rd request onwards: always rate limited
                raise GrowwAPIException(RATE_MSG)
            return good(**kw)
        g.get_historical_candles = first_two_chunks_then_rate_limited

        out = sync_daily(store, source(g, c), ["NSE-X"], NOW, history_start=history_start)
        self.assertIn("NSE-X", out["errors"])
        saved_to = store.last_date("NSE-X")
        self.assertIsNotNone(saved_to, "the two chunks that arrived must have been saved")
        self.assertLess(saved_to, date(2026, 1, 1))            # partial: nowhere near today

        g2 = FakeG(c)                                          # next run: healthy API
        out2 = sync_daily(store, source(g2, c), ["NSE-X"], NOW, history_start=history_start)
        self.assertEqual(out2["errors"], {})
        self.assertGreater(date.fromisoformat(g2.calls[0][2]), history_start, "must resume, not restart from 2024")
        self.assertEqual(store.last_date("NSE-X"), date(2026, 10, 26))
        self.assertFalse(store.load("NSE-X").index.has_duplicates)

    def test_partial_todays_candle_is_still_dropped_before_close(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        early = datetime(2026, 10, 26, 11, 0, tzinfo=IST)               # Monday, before 15:45
        sync_daily(store, source(FakeG(c), c), ["NSE-X"], early, history_start=date(2026, 10, 1))
        self.assertEqual(store.last_date("NSE-X"), date(2026, 10, 23))


class TestAbortRules(unittest.TestCase):
    SYMS = [f"NSE-S{i}" for i in range(8)]

    def test_login_failure_stops_immediately(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c, per_symbol={s: GrowwAPIAuthenticationException("token expired") for s in self.SYMS})
        out = sync_daily(store, source(g, c), self.SYMS, NOW, history_start=date(2026, 9, 1))
        self.assertIn("login", out["aborted"]); self.assertEqual(len(g.calls), 1)
        self.assertEqual(len(out["skipped"]), 7)

    def test_repeated_rate_limit_stops_after_three_stocks_instead_of_hammering_all(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c, per_symbol={s: GrowwAPIException(RATE_MSG) for s in self.SYMS})
        out = sync_daily(store, source(g, c), self.SYMS, NOW, history_start=date(2026, 9, 1))
        self.assertIn("stopped", out["aborted"])
        self.assertEqual(len(out["errors"]), 3); self.assertEqual(len(out["skipped"]), 5)
        self.assertEqual(len({s for _, s, _ in g.calls}), 3)

    def test_a_success_resets_the_streak(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        bad = GrowwAPIException(RATE_MSG)
        g = FakeG(c, per_symbol={self.SYMS[0]: bad, self.SYMS[1]: bad, self.SYMS[3]: bad, self.SYMS[4]: bad})
        out = sync_daily(store, source(g, c), self.SYMS, NOW, history_start=date(2026, 9, 1))
        self.assertIsNone(out["aborted"]); self.assertEqual(len(out["errors"]), 4)

    def test_unknown_symbols_never_abort_the_run(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c, per_symbol={s: GrowwAPIBadRequestException("no such symbol") for s in self.SYMS[:6]})
        out = sync_daily(store, source(g, c), self.SYMS, NOW, history_start=date(2026, 9, 1))
        self.assertIsNone(out["aborted"]); self.assertEqual(len(out["updated"]), 2)

    def test_summary_reports_stats(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c, script=[GrowwAPIException(RATE_MSG)])
        out = sync_daily(store, source(g, c), self.SYMS[:2], NOW, history_start=date(2026, 9, 1))
        self.assertEqual(out["stats"]["rate_limited"], 1); self.assertGreater(out["stats"]["final_pace_s"], 0.6)


class TestInvalidCandles(unittest.TestCase):
    """The first real run: 50 stocks died on `float(None)` from one empty candle each."""

    def test_parser_skips_and_describes_bad_candles_but_keeps_good_ones(self):
        from app.data.groww_source import parse_daily_candles
        skipped = []
        rows = parse_daily_candles({"candles": [
            ["2026-10-20T00:00:00", 10, 11, 9, 10.5, 100, None],
            ["2026-10-21T00:00:00", None, None, None, None, None, None],     # the crash from the server
            ["2026-10-22T00:00:00", 10, "", 9, 10.5, 100, None],              # empty string
            ["2026-10-23T00:00:00", 10, float("nan"), 9, 10.5, 100, None],    # NaN
            ["2026-10-24T00:00:00", 0, 0, 0, 0, 0, None],                     # zero prices
            ["2026-10-25T00:00:00", "12.5", "13", "12", "12.8", "7", None],   # numbers as text are fine
            ["2026-10-26T00:00:00", 10, 11],                                  # truncated row
            [None, 10, 11, 9, 10.5, 1, None],                                 # no timestamp
            ["2026-10-27T00:00:00", 10, 11, 9, 10.5, None, None]]}, skipped)   # missing volume is OK
        self.assertEqual([r[0] for r in rows], ["2026-10-20", "2026-10-25", "2026-10-27"])
        self.assertEqual(rows[1][1:5], (12.5, 13.0, 12.0, 12.8)); self.assertEqual(rows[2][5], 0.0)
        self.assertEqual(len(skipped), 6)
        self.assertTrue(all("missing or invalid" in why or "unreadable" in why for _, why in skipped))

    def test_one_empty_candle_costs_one_day_not_the_whole_stock(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c); g.empty_dates = {"2026-10-21"}
        out = sync_daily(store, source(g, c), ["NSE-X"], NOW, history_start=date(2026, 10, 1))
        self.assertEqual(out["errors"], {}); self.assertIn("NSE-X", out["updated"])
        self.assertEqual(out["dropped"]["NSE-X"][0][0], "2026-10-21")
        self.assertNotIn(date(2026, 10, 21), store.load("NSE-X").index)
        self.assertIn(date(2026, 10, 22), store.load("NSE-X").index)

    def test_candle_that_arrives_empty_is_never_fabricated_or_stored(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c); g.empty_dates = {"2026-10-23"}
        sync_daily(store, source(g, c), ["NSE-X"], NOW, history_start=date(2026, 10, 19))
        self.assertNotIn(date(2026, 10, 23), store.load("NSE-X").index)

    def test_before_the_close_todays_candle_is_not_even_requested(self):
        """09:16 on Monday: today's candle is still forming (and came back empty on the real server)."""
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c); g.empty_dates = {"2026-10-26"}
        early = datetime(2026, 10, 26, 9, 16, tzinfo=IST)
        out = sync_daily(store, source(g, c), ["NSE-X"], early, history_start=date(2026, 10, 1))
        self.assertTrue(all(e <= "2026-10-25" for e in g.ends), g.ends)
        self.assertEqual(out["errors"], {}); self.assertEqual(out["dropped"], {})
        after = datetime(2026, 10, 26, 16, 0, tzinfo=IST)
        g2 = FakeG(c); sync_daily(store, source(g2, c), ["NSE-X"], after, history_start=date(2026, 10, 1))
        self.assertIn("2026-10-26", g2.ends)                             # after the close it is requested

    def test_skipped_candles_do_not_leak_into_the_next_symbol(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c); g.empty_dates = {"2026-10-21"}
        out = sync_daily(store, source(g, c), ["NSE-A", "NSE-B"], NOW, history_start=date(2026, 10, 1))
        self.assertEqual(set(out["dropped"]), {"NSE-A", "NSE-B"})
        self.assertEqual(len(out["dropped"]["NSE-A"]), 1); self.assertEqual(len(out["dropped"]["NSE-B"]), 1)

    def test_skipped_list_of_a_failed_stock_does_not_leak_into_the_next_stock(self):
        c = Clock(); store = CandleStore(tempfile.mktemp())
        g = FakeG(c)
        g.empty_for = {"NSE-A": {"2024-02-01"}}                        # A skips a candle in its first chunk ...
        g.fail_second_call_for = {"NSE-A"}                             # ... then fails on its second request
        out = sync_daily(store, source(g, c), ["NSE-A", "NSE-B"], NOW, history_start=date(2024, 1, 1))
        self.assertIn("NSE-A", out["errors"]); self.assertIn("NSE-B", out["updated"])
        self.assertNotIn("NSE-B", out["dropped"], "B was blamed for a candle that belonged to A")

    def test_summary_output_names_the_skipped_candles(self):
        import io
        from contextlib import redirect_stdout
        tmp = tempfile.mkdtemp()
        summary = {"updated": {"NSE-X": 3}, "errors": {}, "aborted": None, "skipped": [],
                   "dropped": {"NSE-X": [("2026-10-21", "missing or invalid price [None]")]}}
        with mock.patch.object(run, "FETCH_LOCK", f"{tmp}/l"), mock.patch.object(run, "_universe", lambda: []), \
             mock.patch.object(run, "MARKET_DB", f"{tmp}/m.db"), mock.patch.object(run, "_held_symbols", lambda: set()), \
             mock.patch("app.data.groww_source.groww_client_from_env", lambda: object()), \
             mock.patch("app.data.groww_source.GrowwCandleSource", lambda g, **kw: object()), \
             mock.patch("app.data.groww_source.sync_daily", lambda *a, **k: summary):
            buf = io.StringIO()
            with redirect_stdout(buf):
                run._fetch()
        self.assertIn("skipped 1 invalid candle", buf.getvalue()); self.assertIn("2026-10-21", buf.getvalue())

    def test_many_skipped_candles_raise_an_alert_few_do_not(self):
        sent = []
        n = mock.Mock(); n.alert = lambda t: sent.append(t) or True
        with mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: n)):
            run._alert_fetch_errors({"errors": {}, "aborted": None, "dropped": {"NSE-X": [("d", "r")] * 3}})
            self.assertEqual(sent, [])
            run._alert_fetch_errors({"errors": {}, "aborted": None, "dropped": {"NSE-X": [("d", "r")] * 9}})
        self.assertIn("invalid candles", sent[0])


class TestFetchLockAndAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(run, "FETCH_LOCK", f"{self.tmp}/fetch.lock")]
        for x in self.p: x.start()
        self.addCleanup(lambda: [x.stop() for x in self.p])

    def test_second_fetch_is_refused_while_one_holds_the_lock(self):
        with open(run.FETCH_LOCK, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with mock.patch("app.data.groww_source.groww_client_from_env", side_effect=AssertionError("must not log in")):
                with self.assertRaises(run.FetchBusy):
                    run._fetch()
                self.assertEqual(run.cmd_fetch(None), 3)

    def test_daily_job_skips_quietly_and_says_so_while_a_fetch_runs(self):
        sent = []
        n = mock.Mock(); n.send = lambda t: sent.append(t) or True; n.alert = n.send
        with open(run.FETCH_LOCK, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: n)), \
                 mock.patch.object(run, "catch_up", side_effect=AssertionError("must not process on partial data")):
                self.assertEqual(run.cmd_daily(None), 0)
        self.assertTrue(any("still running" in t for t in sent))

    def test_alert_mentions_early_stop_and_stays_silent_on_clean_runs(self):
        sent = []
        n = mock.Mock(); n.alert = lambda t: sent.append(t) or True
        with mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: n)):
            run._alert_fetch_errors({"errors": {}, "aborted": None})
            self.assertEqual(sent, [])
            run._alert_fetch_errors({"errors": {"NSE-X": "e"}, "aborted": "3 stocks in a row failed"})
        self.assertIn("STOPPED EARLY", sent[0])


if __name__ == "__main__":
    unittest.main()
