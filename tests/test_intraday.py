import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

import pandas as pd

from app.config import load_settings
from app.data.intraday_groww import parse_intraday_candles, resolve_five_min_interval
from app.data.intraday_store import IntradayStore
from app.intraday.config import IntradayConfig, load_intraday_config
from app.intraday.engine import INDEX_SYMBOL, gsym, process_session
from app.intraday.orb import evaluate_orb, index_ok
from app.intraday.stats import accounting_check, trade_stats
from app.paper.ledger import Ledger
from app.universe import Constituent

DAY = date(2026, 6, 2)
CFG = IntradayConfig()


def _times(day):
    t0 = datetime(day.year, day.month, day.day, 9, 15)
    return [t0 + timedelta(minutes=5 * i) for i in range(75)]


def _seed(store, day, after, index_trend=0.01):
    """Stock TEST: opening range 100-101 (09:15-09:40), clean breakout bar at 09:45 closing 101.45,
    then after(i) -> (open, high, low, close) for every later bar. NIFTY drifts by index_trend per bar."""
    idx, stk = [], []
    for i, ts in enumerate(_times(day)):
        lab = ts.strftime("%Y-%m-%d %H:%M:%S")
        o, c = 100 + index_trend * max(i - 1, 0), 100 + index_trend * i
        idx.append((lab, o, max(o, c) + 0.02, min(o, c) - 0.02, c, 0))
        if i < 6:
            stk.append((lab, 100.4, 101.0, 100.0, 100.5, 100_000))
        elif i == 6:
            stk.append((lab, 100.9, 101.5, 100.85, 101.45, 400_000))
        else:
            stk.append((lab, *after(i), 100_000))
    store.upsert(INDEX_SYMBOL, idx)
    store.upsert(gsym("TEST"), stk)


def _run(after, index_trend=0.01):
    td = tempfile.mkdtemp()
    store = IntradayStore(os.path.join(td, "m.db"))
    _seed(store, DAY, after, index_trend)
    ledger = Ledger(os.path.join(td, "p.db"), 250_000)
    res = process_session(DAY, ledger, store, [Constituent("TEST", "IT")], load_settings(), CFG)
    return res, ledger


def rally(i):
    o = 101.5 + 0.2 * (i - 7)
    return o, o + 0.25, o - 0.05, o + 0.2


def drop(i):
    return (101.4, 101.45, 100.5, 100.6) if i == 7 else (100.6, 100.7, 100.5, 100.6)


def flat(i):
    return 101.6, 101.7, 101.5, 101.6


def up_then_back(i):
    if i == 7:
        return 101.5, 101.6, 101.4, 101.55
    if i == 8:
        return 101.55, 102.5, 101.7, 102.3
    if i == 9:
        return 102.0, 102.1, 101.0, 101.2
    return 101.2, 101.3, 101.1, 101.2


class ParseTests(unittest.TestCase):
    def test_five_min_interval_new_growwapi(self):
        class G:
            CANDLE_INTERVAL_MIN_5 = "5minute"
        self.assertEqual(resolve_five_min_interval(G()), "5minute")

    def test_parse_epoch(self):
        rows = parse_intraday_candles({"candles": [[1700000000, 10, 11, 9, 10.5, 100]]})
        self.assertEqual(len(rows), 1)

    def test_env_overrides(self):
        cfg = load_intraday_config({"INTRADAY_RISK_PCT": "0.5", "INTRADAY_INDEX_FILTER": "off",
                                    "INTRADAY_ENTRY_END": "10:30"})
        self.assertEqual(cfg.risk_per_trade_pct, 0.5)
        self.assertFalse(cfg.require_index_green)
        self.assertEqual(cfg.entry_end.hour, 10)


class FilterTests(unittest.TestCase):
    def _frames(self, after=flat, upto=6):
        td = tempfile.mkdtemp()
        store = IntradayStore(os.path.join(td, "m.db"))
        _seed(store, DAY, after)
        stk, idx = store.load(gsym("TEST"), day=DAY), store.load(INDEX_SYMBOL, day=DAY)
        return stk.iloc[:upto + 1], idx.iloc[:upto + 1]

    def test_clean_breakout_accepted_with_reasons(self):
        stk, idx = self._frames()
        s = evaluate_orb("TEST", "IT", stk, idx, CFG)
        self.assertFalse(hasattr(s, "reason"), getattr(s, "reason", ""))
        self.assertAlmostEqual(s.stop, round(100.85 - 0.0005 * 101.45, 2))
        self.assertTrue(any("volume" in e for e in s.evidence))

    def test_late_breakout_rejected(self):
        stk, idx = self._frames(after=rally, upto=8)
        out = evaluate_orb("TEST", "IT", stk, idx, CFG)
        self.assertIn("not the first close", out.reason)

    def test_index_filter(self):
        idx = pd.DataFrame({"open": [100] * 8, "high": [100.1] * 8, "low": [99] * 8,
                            "close": [100, 99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3], "volume": [0] * 8},
                           index=[datetime(2026, 6, 2, 9, 15) + timedelta(minutes=5 * i) for i in range(8)])
        ok, why = index_ok(idx, CFG)
        self.assertFalse(ok)
        self.assertIn("below its open", why)


class EngineTests(unittest.TestCase):
    def test_round_trip_returns_cash(self):
        res, led = _run(drop)
        trades = led.trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "INTRADAY_STOP")
        self.assertTrue(accounting_check(led)[0], accounting_check(led)[1])
        self.assertAlmostEqual(res.equity, 250_000 + trades[0]["net"], places=2)
        self.assertGreater(res.equity, 250_000 * 0.99)

    def test_fill_at_next_bar_open(self):
        _, led = _run(rally)
        t = led.trades()[0]
        self.assertAlmostEqual(t["entry"], 101.5 * 1.001, places=2)
        self.assertEqual(t["reason"], "INTRADAY_TARGET")
        self.assertGreater(t["net"], 0)

    def test_breakeven_exit(self):
        res, led = _run(up_then_back)
        self.assertEqual(led.trades()[0]["reason"], "INTRADAY_BREAKEVEN")
        self.assertTrue(any("stop moved to entry" in n for n in res.notes))

    def test_eod_square_off(self):
        _, led = _run(flat)
        t = led.trades()[0]
        self.assertEqual(t["reason"], "INTRADAY_EOD")
        self.assertEqual(led.positions(), [])

    def test_weak_market_blocks_entries(self):
        res, led = _run(rally, index_trend=-0.05)
        self.assertEqual(led.trades(), [])
        self.assertTrue(any("market filter" in n for n in res.notes))

    def test_position_cap(self):
        res, led = _run(drop)
        self.assertLessEqual(res.new_entries[0]["capital_pct_of_equity"], CFG.max_position_pct + 0.01)

    def test_accounting_check_detects_lost_cash(self):
        _, led = _run(drop)
        led.set("cash", led.getf("cash") - 50_000)
        self.assertFalse(accounting_check(led)[0])

    def test_stats(self):
        _, led = _run(rally)
        s = trade_stats(led.trades(), 250_000)
        self.assertEqual(s["wins"], 1)
        self.assertGreater(s["avg_r"], 1.0)


if __name__ == "__main__":
    unittest.main()
