"""Regression tests for review #3 findings."""
import os, shutil, sqlite3, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import app.run as run
from app import backtest as bt
from app.config import Settings
from app.data.store import CandleStore
from app.paper.engine import INDEX_SYMBOL, gsym, process_day
from app.paper.ledger import Ledger
from tests.test_strategy_paper import market

IST = ZoneInfo("Asia/Kolkata")


class TestDroppedFromIndex(unittest.TestCase):
    def test_held_stock_removed_from_nifty_is_still_exited_at_its_stop(self):
        store, uni, days, tmp = market()
        db = tempfile.mktemp(); shutil.copy(f"{tmp}/market.db", db)
        st = CandleStore(db)
        led = Ledger(tempfile.mktemp(), 250000)
        i0 = len(days) - 70
        for k, d in enumerate(days[i0:-3]):
            process_day(d, led, st, uni, Settings())
            if led.positions():
                break
        self.assertTrue(led.positions(), "need a held position for this test")
        p = led.positions()[0]
        nxt = days[i0 + k + 1]
        crash = p["stop"] * 0.97                           # next session opens below the stop
        st.upsert(gsym(p["symbol"]), [(nxt.isoformat(), crash, crash * 1.01, crash * 0.99, crash, 1e6)])
        rebalanced = [c for c in uni if c.symbol != p["symbol"]]
        r = process_day(nxt, led, st, rebalanced, Settings())
        self.assertNotIn(p["symbol"], [x["symbol"] for x in led.positions()])
        self.assertTrue(any(p["symbol"] in e and "STOP_GAP" in e for e in r.exits), r.exits)
        self.assertTrue(any("no longer in the NIFTY 50 list" in n for n in r.notes))

    def test_dropped_stock_keeps_marking_and_gets_no_new_orders(self):
        store, uni, days, _ = market()
        led = Ledger(tempfile.mktemp(), 250000)
        i0 = len(days) - 70
        for k, d in enumerate(days[i0:-3]):
            process_day(d, led, store, uni, Settings())
            if led.positions():
                break
        sym = led.positions()[0]["symbol"]
        rebalanced = [c for c in uni if c.symbol != sym]
        nxt = days[i0 + k + 1]
        r = process_day(nxt, led, store, rebalanced, Settings())
        held = [x for x in led.positions() if x["symbol"] == sym]
        if held:                                           # not exited: price must be today's close
            self.assertAlmostEqual(held[0]["last_close"], float(store.load(gsym(sym), end=nxt)["close"].iloc[-1]))
        self.assertNotIn(sym, [o["symbol"] for o in r.new_orders])


class TestBreadthScope(unittest.TestCase):
    def test_breadth_uses_only_current_index_members(self):
        from app.paper import engine as eng
        from app.analysis.regime import breadth as real_breadth
        store, uni, days, _ = market()
        led = Ledger(tempfile.mktemp(), 250000)
        d0 = days[-5]
        led.con.execute("INSERT INTO positions(symbol,sector,qty,entry,entry_date,stop,target,buy_cost,last_close,"
                        "strategy,regime,risk) VALUES (?,?,1,100,?,1,10000,0,100,'pullback','WEAK_UP',99)",
                        (uni[0].symbol, uni[0].sector, d0.isoformat()))
        seen = []
        with mock.patch.object(eng, "breadth", side_effect=lambda fr: seen.append(set(fr)) or real_breadth(fr)):
            process_day(days[-1], led, store, uni[1:], Settings())      # uni[0] held but dropped from the index
        self.assertEqual(seen[0], {c.symbol for c in uni[1:]})


class TestHeldSymbolsFetched(unittest.TestCase):
    def test_fetch_includes_held_and_pending_symbols(self):
        tmp = tempfile.mkdtemp()
        led = Ledger(f"{tmp}/paper.db", 250000)
        led.con.execute("INSERT INTO positions(symbol,sector,qty,entry,entry_date,stop,target,buy_cost,last_close) "
                        "VALUES ('OLDSTOCK','X',1,10,'2026-01-01',9,12,0,10)")
        led.con.execute("INSERT INTO orders(created_on,symbol,status) VALUES ('2026-01-01','PENDINGCO','PENDING')")
        led.con.commit()
        live = sqlite3.connect(f"{tmp}/live.db")
        live.execute("CREATE TABLE positions(symbol TEXT, status TEXT)")
        live.execute("INSERT INTO positions VALUES ('LIVEONE','OPEN'), ('GONE','CLOSED')"); live.commit(); live.close()
        with mock.patch.object(run, "PAPER_DB", f"{tmp}/paper.db"), mock.patch.object(run, "LIVE_DB", f"{tmp}/live.db"):
            self.assertEqual(run._held_symbols(), {"OLDSTOCK", "PENDINGCO", "LIVEONE"})
            seen = {}
            from tests.synthetic import build
            _, uni_path, _ = build(tmp, n_days=10, n_stocks=50)
            with mock.patch.object(run, "UNIVERSE_CSV", uni_path), mock.patch.object(run, "MARKET_DB", f"{tmp}/m.db"), \
                 mock.patch.object(run, "FETCH_LOCK", f"{tmp}/fetch.lock"), \
                 mock.patch("app.data.groww_source.groww_client_from_env", lambda: object()), \
                 mock.patch("app.data.groww_source.GrowwCandleSource", lambda g, **kw: object()), \
                 mock.patch("app.data.groww_source.sync_daily",
                            lambda store, src, syms, now: seen.setdefault("s", syms) and {"updated": {}, "errors": {}}):
                run._fetch()
        self.assertTrue({"NSE-OLDSTOCK", "NSE-PENDINGCO", "NSE-LIVEONE"} <= set(seen["s"]))
        self.assertIn(INDEX_SYMBOL, seen["s"])


class TestWalkForwardFolds(unittest.TestCase):
    def test_folds_do_not_share_days(self):
        store, uni, days, _ = market()
        cache = bt.CachedStore(store, [INDEX_SYMBOL] + [gsym(c.symbol) for c in uni])
        tdays = set(days)
        # pick a start whose fold boundary is a trading day, so an overlap would really be double-counted
        start = next(d for d in days[-90:-40] if bt.add_months(d, 1) in tdays)
        end = days[-1]
        folds = bt.walk_forward(cache, uni, start, end, Settings(), 250000, months=1)
        self.assertGreaterEqual(len(folds), 2)
        self.assertEqual(sum(f["days"] for f in folds), len(cache.trading_days(start, end)))
        self.assertIn("results dates are not available", bt.render({}, None, [], {}, {}, 1))


class TestDailyAlertsOnce(unittest.TestCase):
    def run_daily(self, now):
        alerts = []
        fake = mock.Mock(); fake.alert = lambda t: alerts.append(t) or True; fake.send = lambda t: True
        calls = []
        class FixedDT(datetime):
            @classmethod
            def now(cls, tz=None): return now
        with mock.patch.object(run, "_fetch", lambda: calls.append(1) or {"errors": {"NSE-X": "503"}, "updated": {}}), \
             mock.patch.object(run, "catch_up", lambda *a, **k: 0), mock.patch.object(run, "datetime", FixedDT), \
             mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: fake)), \
             mock.patch.object(run, "MARKET_DB", self.db), \
             mock.patch.dict(os.environ, {"DAILY_RETRIES": "2", "DAILY_RETRY_WAIT": "0", "HEARTBEAT_URL": ""}):
            rc = run.cmd_daily(None)
        return rc, len(calls), [a for a in alerts if "Data fetch" in a]

    def setUp(self):
        self.db = tempfile.mktemp()
        st = CandleStore(self.db)
        st.upsert(INDEX_SYMBOL, [("2026-10-23", 1, 1, 1, 1, 1)])           # a Friday

    def test_errors_alerted_when_candle_arrives_first_try(self):
        rc, fetches, alerts = self.run_daily(datetime(2026, 10, 23, 17, 0, tzinfo=IST))
        self.assertEqual((rc, fetches, len(alerts)), (0, 1, 1))

    def test_errors_alerted_once_after_all_retries(self):
        rc, fetches, alerts = self.run_daily(datetime(2026, 10, 26, 17, 0, tzinfo=IST))   # Monday, no candle
        self.assertEqual((fetches, len(alerts)), (3, 1))


class TestDashboardUnit(unittest.TestCase):
    def test_restart_is_rate_limited(self):
        t = Path("deploy/systemd/groww-agent-dashboard.service").read_text()
        self.assertIn("StartLimitBurst=5", t); self.assertIn("StartLimitIntervalSec=300", t)


if __name__ == "__main__":
    unittest.main()
