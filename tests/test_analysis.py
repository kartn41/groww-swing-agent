import unittest
from datetime import date
import numpy as np
import pandas as pd
from app.analysis.indicators import atr, ema, relative_volume, rsi, sma
from app.analysis.levels import levels_as_of, swing_points
from app.analysis.regime import breadth, classify
from tests.synthetic import business_days, make_series


def frame(n=300, seed=3, drift=0.0006, vol=0.012):
    days = business_days(date(2025, 1, 1), n)
    df = pd.DataFrame(make_series(days, seed, drift, vol, 100),
                      columns=["d", "open", "high", "low", "close", "volume"])
    df["d"] = pd.to_datetime(df["d"]).dt.date
    return df.set_index("d")


class TestIndicators(unittest.TestCase):
    def test_sma_ema(self):
        s = pd.Series([1.0, 2, 3, 4, 5])
        self.assertTrue(np.isnan(sma(s, 3).iloc[1])); self.assertEqual(sma(s, 3).iloc[-1], 4.0)
        self.assertAlmostEqual(ema(pd.Series([5.0] * 30), 10).iloc[-1], 5.0)

    def test_rsi_extremes(self):
        self.assertEqual(rsi(pd.Series(np.arange(1.0, 40.0)), 14).iloc[-1], 100.0)
        self.assertLess(rsi(pd.Series(np.arange(40.0, 1.0, -1)), 14).iloc[-1], 1.0)

    def test_atr_constant_range(self):
        df = pd.DataFrame({"high": [11.0] * 40, "low": [9.0] * 40, "close": [10.0] * 40})
        self.assertAlmostEqual(atr(df, 14).iloc[-1], 2.0)

    def test_relative_volume_excludes_today(self):
        v = pd.Series([100.0] * 25 + [300.0])
        self.assertAlmostEqual(relative_volume(v, 20).iloc[-1], 3.0)


class TestLevelsNoLookAhead(unittest.TestCase):
    def test_swing_detection(self):
        h = [1, 2, 3, 9, 3, 2, 1, 2, 3, 4]
        df = pd.DataFrame({"high": h, "low": [x - 0.5 for x in h]})
        sw = [s for s in swing_points(df, k=3) if s.kind == "H"]
        self.assertEqual(len(sw), 1); self.assertEqual((sw[0].bar, sw[0].known_at), (3, 6))

    def test_levels_identical_with_or_without_future_bars(self):
        df = frame()
        a = atr(df, 14)
        for t in (230, 250, 270, 290):
            full = levels_as_of(df, t, float(a.iloc[t]))
            prefix = levels_as_of(df.iloc[: t + 1], t, float(a.iloc[t]))
            self.assertEqual([(z.low, z.high, z.touches) for z in full.support + full.resistance],
                             [(z.low, z.high, z.touches) for z in prefix.support + prefix.resistance], t)


class TestRegime(unittest.TestCase):
    def test_unknown_when_data_missing(self):
        self.assertEqual(classify(frame(100), 60).name, "UNKNOWN")
        self.assertEqual(classify(frame(), None).name, "UNKNOWN")
        self.assertFalse(classify(frame(), None).allows_new_longs)

    def test_uptrend_and_downtrend(self):
        up = classify(frame(drift=0.002, vol=0.006), 70)
        self.assertIn(up.name, ("STRONG_UP", "WEAK_UP")); self.assertTrue(up.allows_new_longs)
        dn = classify(frame(drift=-0.002, vol=0.006), 20)
        self.assertIn(dn.name, ("STRONG_DOWN", "WEAK_DOWN")); self.assertFalse(dn.allows_new_longs)

    def test_volatility_spike_overrides(self):
        df = frame(drift=0.002, vol=0.006)
        df.iloc[-15:, df.columns.get_loc("high")] *= 1.06
        df.iloc[-15:, df.columns.get_loc("low")] *= 0.94
        self.assertEqual(classify(df, 70).name, "HIGH_VOLATILITY")

    def test_breadth_needs_enough_symbols(self):
        self.assertIsNone(breadth({f"S{i}": frame(120, seed=i) for i in range(5)}))
        b = breadth({f"S{i}": frame(120, seed=i) for i in range(25)})
        self.assertTrue(0 <= b <= 100)


if __name__ == "__main__":
    unittest.main()
