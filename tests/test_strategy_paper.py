import os, tempfile, unittest
from datetime import date
from unittest import mock
import pandas as pd
from app.analysis.indicators import sma
from app.config import Settings, RiskLimits
from app.paper import engine as eng
from app.paper.engine import gsym, process_day
from app.paper.ledger import AlreadyProcessed, Ledger
from app.paper.report import render, summary
from app.paper.simulator import Bar, entry_fill, exit_check
from app.strategies import pullback
from app.universe import load_universe
from tests.synthetic import build

_CACHE = {}


def market():
    if "m" not in _CACHE:
        tmp = tempfile.mkdtemp()
        store, uni_path, days = build(tmp, n_days=330)
        _CACHE["m"] = (store, load_universe(uni_path)[0], days, tmp)
    return _CACHE["m"]


def replay(n_days=50, settings=None, kill=False, db=None):
    store, uni, days, tmp = market()
    led = Ledger(db or tempfile.mktemp(), 250000)
    results = [process_day(d, led, store, uni, settings or Settings(), kill_switch=kill) for d in days[-n_days:]]
    return led, results


class TestSimulator(unittest.TestCase):
    def test_entry_rules(self):
        self.assertIsNone(entry_fill(Bar(101, 102, 100, 101), 100, 0.5)[0])     # gapped >0.5%: no chase
        self.assertEqual(entry_fill(Bar(99, 101, 98, 100), 100, 0.5)[0], 99)    # open below limit
        self.assertEqual(entry_fill(Bar(100.3, 101, 99.5, 100), 100, 0.5)[0], 100)  # intraday at limit
        self.assertIsNone(entry_fill(Bar(100.3, 101, 100.1, 100.5), 100, 0.5)[0])

    def test_exit_rules_are_conservative(self):
        self.assertEqual(exit_check(Bar(90, 92, 89, 91), 95, 110, False), ("STOP_GAP", 90))
        self.assertEqual(exit_check(Bar(100, 111, 94, 105), 95, 110, False), ("STOP", 95))  # both hit
        self.assertEqual(exit_check(Bar(100, 111, 96, 105), 95, 110, False), ("TARGET", 110))
        self.assertEqual(exit_check(Bar(100, 111, 96, 105), 95, 110, True), (None, None))    # entry day
        self.assertEqual(exit_check(Bar(112, 115, 111, 114), 95, 110, False), ("TARGET_GAP", 112))


class TestStrategy(unittest.TestCase):
    def test_finds_valid_setups_with_consistent_plans(self):
        store, uni, days, _ = market()
        idx = store.load("NSE-NIFTY")
        found = 0
        for c in uni[:20]:
            df = store.load(gsym(c.symbol))
            for t in range(240, len(df), 3):
                out = pullback.evaluate(c.symbol, c.sector, df.iloc[: t + 1], idx.iloc[: t + 1], 15)
                if isinstance(out, pullback.Setup):
                    found += 1
                    self.assertLess(out.stop, out.entry); self.assertLess(out.entry, out.target)
                    self.assertGreaterEqual(out.gross_rr, 2.2)
                    self.assertGreater(out.entry, sma(df["close"].iloc[: t + 1], 50).iloc[-1])
                    self.assertTrue(0 <= out.score <= 100); self.assertTrue(out.evidence)
        self.assertGreater(found, 0, "synthetic market should contain some pullback setups")

    def test_volume_gate_rejects_quiet_bounces_and_heavy_dips(self):
        store, uni, days, _ = market()
        idx = store.load("NSE-NIFTY")
        found = None
        with mock.patch.dict(os.environ, {"PULLBACK_VOLUME_GATE": "off"}):
            for c in uni[:20]:
                df = store.load(gsym(c.symbol))
                for t in range(240, len(df), 3):
                    if isinstance(pullback.evaluate("X", "S", df.iloc[: t + 1], idx.iloc[: t + 1], 15), pullback.Setup):
                        found = (df.iloc[: t + 1].copy(), idx.iloc[: t + 1])
                        break
                if found:
                    break
        self.assertIsNotNone(found)
        d, i = found

        def with_volume(dip, bounce):
            x = d.copy()
            x["volume"] = 3e6
            x.iloc[-5:-1, x.columns.get_loc("volume")] = dip
            x.iloc[-1, x.columns.get_loc("volume")] = bounce
            return pullback.evaluate("X", "S", x, i, 15)

        with mock.patch.dict(os.environ, {"PULLBACK_VOLUME_GATE": "on"}):
            self.assertIsInstance(with_volume(dip=2e6, bounce=4e6), pullback.Setup)
            quiet = with_volume(dip=2e6, bounce=2.5e6)
            self.assertIsInstance(quiet, pullback.Rejection)
            self.assertIn("bounce volume", quiet.reason)
            heavy = with_volume(dip=3.5e6, bounce=4e6)
            self.assertIsInstance(heavy, pullback.Rejection)
            self.assertIn("did not dry up", heavy.reason)
        with mock.patch.dict(os.environ, {"PULLBACK_VOLUME_GATE": "off"}):
            self.assertIsInstance(with_volume(dip=3.5e6, bounce=2.5e6), pullback.Setup)
        with mock.patch.dict(os.environ):
            os.environ.pop("PULLBACK_VOLUME_GATE", None)
            self.assertIsInstance(with_volume(dip=3.5e6, bounce=2.5e6), pullback.Setup)   # off by default

    def test_rejects_downtrend_and_short_history(self):
        store, uni, days, _ = market()
        df = store.load(gsym(uni[0].symbol))
        down = df.iloc[::-1].copy(); down.index = df.index      # mirror the series in time
        r = pullback.evaluate("X", "S", down, store.load("NSE-NIFTY"), 15)
        self.assertIsInstance(r, pullback.Rejection)
        self.assertIsInstance(pullback.evaluate("X", "S", df.iloc[:100], store.load("NSE-NIFTY"), 15),
                              pullback.Rejection)


def crafted_frame(n=300, slope=0.4, drop=0.06, droplen=3, bounce=0.008):
    """Steady uptrend, shallow pullback, bounce that still closes BELOW the 50-day average."""
    import numpy as np
    from tests.synthetic import business_days
    days = business_days(date(2025, 1, 1), n)
    close = 100 + slope * np.arange(n)
    peak = n - 1 - droplen - 1
    for k in range(1, droplen + 1):
        close[peak + k] = close[peak] * (1 - drop * k / droplen)
    close[-1] = close[-2] * (1 + bounce)
    opn = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * 1.003,
                         "low": np.minimum(opn, close) * 0.997, "close": close, "volume": 3e6}, index=days)


class TestPullbackEntryGuards(unittest.TestCase):
    def test_close_below_sma50_is_rejected_because_exit_rule_would_fire_at_once(self):
        df = crafted_frame()
        self.assertLessEqual(df["close"].iloc[-1], sma(df["close"], 50).iloc[-1])   # precondition
        self.assertGreater(df["close"].iloc[-1], sma(df["close"], 200).iloc[-1])
        idx = crafted_frame(slope=0.0, drop=0.0); idx.iloc[:, :4] = 20000.0
        out = pullback.evaluate("T", "S", df, idx, 15)
        self.assertIsInstance(out, pullback.Rejection)
        self.assertIn("close below SMA50", out.reason)


class TestPaperEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.led, cls.results = replay(70)

    def test_trades_happen_and_orders_fill_only_after_decision_day(self):
        orders = self.led.con.execute("SELECT * FROM orders").fetchall()
        self.assertTrue(orders, "expected some orders in 70 synthetic days")
        for o in self.led.con.execute("SELECT o.created_on, p.entry_date FROM orders o JOIN "
                                      "(SELECT symbol, entry_date FROM trades UNION SELECT symbol, entry_date FROM positions) p "
                                      "ON o.symbol=p.symbol AND o.status='FILLED'").fetchall():
            pass
        for t in self.led.trades():
            created = self.led.con.execute("SELECT MAX(created_on) FROM orders WHERE symbol=? AND created_on<?",
                                           (t["symbol"], t["entry_date"])).fetchone()[0]
            self.assertIsNotNone(created); self.assertLess(created, t["entry_date"])

    def test_accounting_identity(self):
        start = self.led.getf("start_capital")
        closed = sum(t["net"] for t in self.led.trades())
        open_cost = sum(p["entry"] * p["qty"] + p["buy_cost"] for p in self.led.positions())
        self.assertAlmostEqual(self.led.getf("cash"), start + closed - open_cost, places=4)

    def test_limits_respected_every_day(self):
        self.assertGreaterEqual(self.led.con.execute("SELECT MIN(cash) FROM equity").fetchone()[0], 0)
        self.assertLessEqual(len(self.led.positions()), RiskLimits().max_positions)

    def test_same_day_twice_refused_and_report_renders(self):
        store, uni, days, _ = market()
        with self.assertRaises(AlreadyProcessed):
            process_day(days[-1], self.led, store, uni, Settings())
        text = render(self.results[-1], self.led)
        self.assertIn("PAPER report", text); self.assertIn("no real orders", text)
        self.assertIn("Days processed", summary(self.led))

    def test_non_trading_day_skipped(self):
        store, uni, days, _ = market()
        led = Ledger(tempfile.mktemp(), 250000)
        r = process_day(date(2026, 12, 25) if date(2026, 12, 25) not in days else date(1999, 1, 1),
                        led, store, uni, Settings())
        self.assertTrue(r.skipped); self.assertIsNone(led.get("last_processed"))


class TestPaperSafety(unittest.TestCase):
    def test_kill_switch_blocks_orders_but_not_exits(self):
        led, res = replay(70, kill=True)
        self.assertEqual(led.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertTrue(all("KILL SWITCH" in " ".join(r.notes) for r in res if not r.skipped))

    def test_crash_mid_day_rolls_back(self):
        store, uni, days, _ = market()
        led = Ledger(tempfile.mktemp(), 250000)
        process_day(days[-3], led, store, uni, Settings())
        before = (led.get("last_processed"), led.getf("cash"),
                  led.con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        with mock.patch.object(eng, "classify", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                process_day(days[-2], led, store, uni, Settings())
        after = (led.get("last_processed"), led.getf("cash"),
                 led.con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(before, after)
        process_day(days[-2], led, store, uni, Settings())       # and the day can be retried cleanly

    def test_ledger_survives_restart(self):
        db = tempfile.mktemp()
        led, _ = replay(40, db=db)
        cash, last = led.getf("cash"), led.get("last_processed")
        led.con.close()
        again = Ledger(db, 999)                                   # capital arg ignored for existing ledger
        self.assertEqual((again.getf("cash"), again.get("last_processed")), (cash, last))

    def test_bad_data_symbol_excluded(self):
        store, uni, days, tmp = market()
        bad = uni[5].symbol
        df = store.load(gsym(bad))
        rows = [(d.isoformat(), r.open / 2, r.high / 2, r.low / 2, r.close / 2, r.volume)
                for d, r in df.iloc[-15:].iterrows()]
        from app.data.store import CandleStore
        import shutil
        db2 = tempfile.mktemp(); shutil.copy(f"{tmp}/market.db", db2)
        st2 = CandleStore(db2); st2.upsert(gsym(bad), rows)
        r = process_day(days[-1], Ledger(tempfile.mktemp(), 250000), st2, uni, Settings())
        self.assertIn(bad, r.data_issues)
        if r.rejections:
            self.assertTrue(r.rejections.get(bad, "").startswith("data quality"))


if __name__ == "__main__":
    unittest.main()
