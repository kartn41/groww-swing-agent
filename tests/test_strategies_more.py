import unittest
from app.strategies import REGIME_STRATEGIES, STRATEGIES, breakout, permitted, rs_continuation, vcp
from app.strategies.common import Rejection, Setup
from tests import crafted as C

IDX = C.flat_index()


def ev(fn, df, **kw):
    return fn("T", "S", df, IDX, 15, **kw)


class TestPatterns(unittest.TestCase):
    def check_setup(self, out):
        self.assertIsInstance(out, Setup, getattr(out, "reason", ""))
        self.assertLess(out.stop, out.entry); self.assertLess(out.entry, out.target)
        self.assertGreaterEqual(out.gross_rr, 2.2); self.assertLessEqual((out.entry - out.stop) / out.entry, 0.08)
        self.assertTrue(out.evidence); self.assertTrue(0 <= out.score <= 100)

    def test_breakout(self):
        self.check_setup(ev(breakout.evaluate, C.breakout()))
        self.assertIn("volume", ev(breakout.evaluate, C.breakout(last_vol_mult=1.1)).reason)
        self.assertIn("not chasing", ev(breakout.evaluate, C.breakout(last_jump=0.06)).reason)

    def test_rs_continuation(self):
        self.check_setup(ev(rs_continuation.evaluate, C.rs_leader(), sector_rs=0.02))
        self.assertIn("resumption", ev(rs_continuation.evaluate, C.rs_leader(last_vol_mult=1.0)).reason)
        self.assertIn("sector lagging", ev(rs_continuation.evaluate, C.rs_leader(), sector_rs=-0.03).reason)

    def test_vcp(self):
        out = ev(vcp.evaluate, C.vcp())
        self.check_setup(out)
        self.assertIn("pivot", ev(vcp.evaluate, C.vcp(last_vol_mult=0.5)).reason)
        self.assertIn("contraction", ev(vcp.evaluate, C.vcp(ranges=(0.2, 0.15, 0.12))).reason)

    def test_every_strategy_rejects_short_history(self):
        for n, fn in STRATEGIES.items():
            self.assertIsInstance(fn("T", "S", C.breakout().iloc[:150], IDX, 15), Rejection, n)


class TestRegimeRules(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(set(permitted("STRONG_UP")), set(STRATEGIES))
        self.assertEqual(permitted("WEAK_UP"), ["pullback", "vcp"])
        for r in ("SIDEWAYS", "WEAK_DOWN", "STRONG_DOWN", "HIGH_VOLATILITY", "UNKNOWN"):
            self.assertEqual(permitted(r), [], r)
        self.assertEqual(permitted("STRONG_UP", ["vcp"]), ["vcp"])
        self.assertTrue(set(sum(REGIME_STRATEGIES.values(), [])) <= set(STRATEGIES))


if __name__ == "__main__":
    unittest.main()
