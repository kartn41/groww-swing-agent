import tempfile, unittest
from app import backtest as bt
from app.config import Settings
from app.paper.engine import INDEX_SYMBOL, gsym
from tests.test_strategy_paper import market


class TestBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cls.uni, cls.days, _ = market()
        cls.cache = bt.CachedStore(store, [INDEX_SYMBOL] + [gsym(c.symbol) for c in cls.uni])
        cls.start, cls.end = cls.days[-90], cls.days[-1]
        cls.led = bt.run(cls.cache, cls.uni, cls.start, cls.end, Settings(), 250000)
        cls.st = bt.stats(cls.led)

    def test_cache_never_returns_future_bars(self):
        d = self.days[-50]
        df = self.cache.load(INDEX_SYMBOL, end=d)
        self.assertEqual(df.index[-1], d); self.assertLessEqual(len(df), self.cache.tail)

    def test_same_engine_as_paper(self):
        from app.paper.engine import process_day
        from app.paper.ledger import Ledger
        led = Ledger(":memory:", 250000)
        for d in self.cache.trading_days(self.start, self.end):
            process_day(d, led, self.cache, self.uni, Settings())
        self.assertEqual(led.getf("cash"), self.led.getf("cash"))

    def test_statistics_are_consistent(self):
        st = self.st
        self.assertEqual(st["days"], 90)
        self.assertGreaterEqual(st["max_drawdown"], 0)
        if st["trades"]:
            self.assertAlmostEqual(sum(v["trades"] for v in st["by_strategy"].values()), st["trades"])
            self.assertTrue(0 <= st["win_rate"] <= 1)
            lo, hi = st["expectancy_r_ci95"] or (st["expectancy_r"], st["expectancy_r"])
            self.assertLessEqual(lo, hi)

    def test_walk_forward_folds_cover_period(self):
        folds = bt.walk_forward(self.cache, self.uni, self.start, self.end, Settings(), 250000, months=2)
        self.assertGreaterEqual(len(folds), 2)
        self.assertEqual(folds[0]["fold"].split(" to ")[0], str(self.start))

    def test_report_mentions_bias_and_flags(self):
        text = bt.render(self.st, None, [], {}, {}, 250000)
        self.assertIn("survivorship", text); self.assertIn("Red flags", text)
        self.assertTrue(any("trades" in f for f in bt.verdict({"trades": 3}, None, [])))

    def test_sensitivity_variants_change_settings(self):
        s = bt.sensitivity(self.cache, self.uni, self.days[-30], self.end, Settings(), 250000)
        self.assertEqual(set(s), set(bt.SENSITIVITY))


if __name__ == "__main__":
    unittest.main()
