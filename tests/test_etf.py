"""GoldBEES / SilverBEES strategy: indicators, analysis, rules, sizing, ledger, engine, market-hours cycle, live execution."""
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.analysis.indicators import adx, bollinger, macd
from app.broker.paper import PaperBroker
from app.etf import analysis as an
from app.etf.analysis import Snapshot, analyse, sanitize
from app.etf.config import EtfConfig, load_etf_config
from app.etf.engine import buy_ref, end_of_day, simulate_session
from app.etf.intraday import LiveExecution, PaperExecution, hourly_confirms, m15_timing, run_cycle
from app.etf.ledger import EtfLedger
from app.etf.market import MarketDataError, Quote, parse_candles, parse_quote
from app.etf.report import format_cycle, format_eod
from app.etf.strategy import PositionView, Rejection, entry_signal, review, size, sudden_drop_level

IST = ZoneInfo("Asia/Kolkata")
CFG = EtfConfig()
GOLD = CFG.profile("GOLDBEES")
SILVER = CFG.profile("SILVERBEES")
D0 = date(2026, 11, 2)            # Monday


def daily(n=300, start=100.0, drift=0.001, vol=0.008, seed=1, end=D0):
    rng = np.random.default_rng(seed)
    c = start * np.cumprod(1 + drift + rng.normal(0, vol, n))
    o = np.r_[start, c[:-1]] * (1 + rng.normal(0, 0.001, n))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.003, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.003, n)))
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n).date
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e6}, index=idx)


def snap(**kw) -> Snapshot:
    base = dict(symbol="GOLDBEES", d=D0, open=99.0, high=100.6, low=98.8, close=100.5, prev_close=99.2,
                prev_high=100.2, ema={5: 100.0, 9: 100.3, 20: 100.0, 50: 97.0, 200: 90.0}, slope50=0.02, adx=26.0,
                pdi=24.0, mdi=18.0, rsi=50.0, rsi_prev=45.0, rsi_min5=42.0, macd=0.1, macd_signal=0.05, hist=0.05,
                hist_prev=-0.02, hist_prev2=-0.05, roc10=-0.01, rv=1.2, atr=1.0, atr_pct=0.01, atr_pct_median=0.01,
                bb_lower=98.5, bb_upper=104.0, pct_b=0.3, moves={2: -0.005, 3: -0.01, 5: -0.02, 10: -0.01},
                swing_high=103.0, pullback_low=99.0, depth_pct=0.039, depth_atr=4.0 / 1.0)
    base.update(kw)
    s = Snapshot(**base)
    s.depth_atr = (s.swing_high - s.pullback_low) / s.atr
    s.depth_pct = (s.swing_high - s.pullback_low) / s.swing_high
    s.regime, s.regime_detail = kw.get("regime") or an.classify_regime(s), "test"
    if "regime" in kw:
        s.regime = kw["regime"]
    s.confirmations = an.reversal_confirmations(s)
    s.pullback = kw.get("pullback") or an.classify_pullback(s)
    return s


def pos(**kw) -> PositionView:
    base = dict(symbol="GOLDBEES", qty=100, avg=100.0, stop=97.0, target=102.0, tranches=1, last_tranche_price=100.0,
                sessions_since_tranche=5, sessions_held=5, peak_close=100.0, planned_value=60000.0)
    base.update(kw)
    return PositionView(**base)


class Indicators(unittest.TestCase):
    def test_adx_high_in_trend_low_in_chop(self):
        up = daily(drift=0.006, vol=0.002)
        chop = daily(drift=0.0, vol=0.008, seed=3)
        self.assertGreater(adx(up)["adx"].iloc[-1], 40)
        self.assertGreater(adx(up)["pdi"].iloc[-1], adx(up)["mdi"].iloc[-1])
        self.assertLess(adx(chop)["adx"].iloc[-1], adx(up)["adx"].iloc[-1])

    def test_macd_and_bollinger_shapes(self):
        df = daily()
        m, b = macd(df["close"]), bollinger(df["close"])
        self.assertAlmostEqual(float(m["hist"].iloc[-1]), float(m["macd"].iloc[-1] - m["signal"].iloc[-1]))
        self.assertTrue((b["upper"].dropna() >= b["lower"].dropna()).all())

    def test_no_lookahead(self):
        df = daily()
        a = analyse("GOLDBEES", df.iloc[:-5])
        df2 = df.copy()
        df2.iloc[-5:, :] = df2.iloc[-5:, :] * 3        # change the future only
        self.assertEqual(analyse("GOLDBEES", df2.iloc[:-5]).summary(), a.summary())


class Sanitize(unittest.TestCase):
    def test_bad_open_print_repaired_real_gap_kept(self):
        df = daily(n=60, vol=0.004)
        d1, d2 = df.index[40], df.index[50]
        pc = df["close"].iloc[39]
        df.loc[d1, ["open", "low"]] = pc * 0.88                         # freak print, close normal
        df.loc[d1, "close"] = pc * 1.001
        df.loc[d1, "high"] = pc * 1.004
        pc2 = df["close"].iloc[49]
        df.loc[d2, ["open", "high", "low", "close"]] = [pc2 * 0.9, pc2 * 0.905, pc2 * 0.85, pc2 * 0.86]   # real gap
        out, n = sanitize(df)
        self.assertGreaterEqual(n, 1)
        self.assertAlmostEqual(out.loc[d1, "open"], pc)
        self.assertGreater(out.loc[d1, "low"], pc * 0.95)
        self.assertEqual(out.loc[d2, "open"], df.loc[d2, "open"])
        self.assertEqual(out.loc[d2, "low"], df.loc[d2, "low"])


class Regime(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(analyse("G", daily(drift=0.004, vol=0.004)).regime, an.STRONG_BULL)
        self.assertEqual(analyse("G", daily(drift=-0.004, vol=0.004)).regime, an.STRONG_BEAR)

    def test_insufficient_history(self):
        with self.assertRaises(an.InsufficientData):
            analyse("G", daily(n=100))


class Entry(unittest.TestCase):
    def test_confirmed_dip_in_uptrend_is_a_signal(self):
        s = snap(regime=an.STRONG_BULL)
        out = entry_signal(s, GOLD, CFG)
        self.assertNotIsInstance(out, Rejection, getattr(out, "reason", ""))
        self.assertEqual((out.tranche, out.kind), (1, "INITIAL"))
        self.assertLess(out.stop, s.pullback_low)
        self.assertGreaterEqual(out.targets.preferred, CFG.preferred_target_pct / 100)
        self.assertGreaterEqual(out.targets.extended, out.targets.preferred)

    def test_strong_bear_never_buys(self):
        out = entry_signal(snap(regime=an.STRONG_BEAR, pullback=an.POSSIBLE_REVERSAL), GOLD, CFG)
        self.assertIsInstance(out, Rejection)
        self.assertIn("preserving capital", out.reason)

    def test_bearish_momentum_without_reversal_rejected(self):
        out = entry_signal(snap(regime=an.MILD_BULL, pullback=an.BEARISH_MOMENTUM), GOLD, CFG)
        self.assertIn("bearish momentum", out.reason)

    def test_price_drop_alone_is_not_enough(self):
        s = snap(regime=an.STRONG_BULL, hist=-0.1, hist_prev=-0.05, macd=-0.2, macd_signal=-0.1, close=99.1,
                 open=99.5, rsi=40, rsi_prev=42, rv=0.8, high=99.6, low=99.0, pullback=an.HEALTHY_CORRECTION)
        self.assertEqual(s.confirmations, [])
        out = entry_signal(s, GOLD, CFG)
        self.assertIsInstance(out, Rejection)
        self.assertIn("confirmation", out.reason)

    def test_no_averaging_down_in_bear_regime(self):
        out = entry_signal(snap(regime=an.MILD_BEAR, pullback=an.POSSIBLE_REVERSAL), GOLD, CFG,
                           pos(last_tranche_price=103.0, stop=95.0))
        self.assertIn("no averaging down", out.reason)

    def test_second_tranche_needs_better_price_or_continuation(self):
        s = snap(regime=an.STRONG_BULL, close=100.5)
        self.assertIn("no new favourable price", entry_signal(s, GOLD, CFG, pos(avg=101.0, last_tranche_price=100.8,
                                                                                 stop=96.0)).reason)
        ok = entry_signal(s, GOLD, CFG, pos(avg=102.0, last_tranche_price=102.0, stop=96.0))
        self.assertEqual((ok.tranche, ok.kind), (2, "DIP_ADD"))

    def test_tranche_spacing_and_cap(self):
        s = snap(regime=an.STRONG_BULL)
        self.assertIn("session", entry_signal(s, GOLD, CFG, pos(sessions_since_tranche=1, last_tranche_price=103)).reason)
        self.assertIn("all 3", entry_signal(s, GOLD, CFG, pos(tranches=3)).reason)

    def test_cooldown(self):
        self.assertIn("cool-down", entry_signal(snap(regime=an.STRONG_BULL), GOLD, CFG, cooldown=True).reason)

    def test_aggressive_trend_initial_without_deep_dip(self):
        aggr = load_etf_config({"ETF_STRATEGY_MODE": "aggressive", "ETF_SYMBOLS": "GOLDBEES"})
        s = snap(regime=an.STRONG_BULL, pullback=an.NO_PULLBACK, depth_atr=0.4, depth_pct=0.004,
                 slope50=0.03, adx=28, hist=0.08, hist_prev=0.05, roc10=0.02, close=102.0,
                 ema={5: 101.5, 9: 101.2, 20: 100.5, 50: 99.0, 200: 90.0}, rsi=55, pct_b=0.6,
                 swing_high=102.5, pullback_low=101.8)
        out = entry_signal(s, aggr.profile("GOLDBEES"), aggr)
        self.assertNotIsInstance(out, Rejection, getattr(out, "reason", ""))
        self.assertEqual(out.kind, "TREND_INITIAL")


class Sizing(unittest.TestCase):
    def sig(self, **kw):
        s = snap(regime=an.STRONG_BULL, pullback_low=99.5, **kw)
        return s, entry_signal(s, GOLD, CFG)

    def test_tranche1_is_40pct_of_allocation_and_within_loss_budget(self):
        s, g = self.sig()
        z = size(g, GOLD, CFG, 100000, 100000, {}, None)
        self.assertGreater(z.qty, 0, z.reasons)
        self.assertLessEqual(z.qty * g.limit, 100000 * GOLD.max_exposure_pct / 100 * 0.4 * 1.001)
        self.assertLessEqual(z.risk_amount, 100000 * GOLD.max_loss_pct / 100 + 1e-6)

    def test_cash_and_exposure_caps(self):
        s, g = self.sig()
        tiny = size(g, GOLD, CFG, 100000, 500, {}, None)            # 4 units: fixed charges eat the profit
        self.assertEqual(tiny.qty, 0)
        self.assertIn("does not cover costs", tiny.reasons[0])
        z = size(g, GOLD, CFG, 100000, 20000, {}, None)
        self.assertTrue(0 < z.qty <= 20000 / g.limit)
        self.assertEqual(size(g, GOLD, CFG, 100000, 100000, {"GOLDBEES": 60000}, None).qty, 0)
        z = size(g, GOLD, CFG, 100000, 100000, {"SILVERBEES": 79000}, None)
        self.assertLessEqual(z.qty * g.limit, 1000 * 1.01)

    def test_reward_must_justify_risk_after_costs(self):
        s, g = self.sig()
        z = size(g, GOLD, replace(CFG, min_net_rr=5.0), 100000, 100000, {}, None)
        self.assertEqual(z.qty, 0)
        self.assertIn("net reward/risk", z.reasons[0])

    def test_silver_tighter_loss_budget(self):
        self.assertLess(SILVER.max_loss_pct, GOLD.max_loss_pct)
        self.assertLess(SILVER.max_exposure_pct, GOLD.max_exposure_pct)


class Exits(unittest.TestCase):
    def test_trailing_stop_and_extension(self):
        s = snap(regime=an.STRONG_BULL, close=103.0, adx=30, hist=0.2, hist_prev=0.1, rsi=62, rsi_prev=60,
                 ema={5: 102, 9: 102, 20: 101, 50: 98, 200: 90}, macd=0.5, macd_signal=0.3)
        r = review(pos(peak_close=103.0), s, GOLD, CFG)
        self.assertGreater(r.stop, 100.0)                     # profit protected
        self.assertTrue(r.extended)
        self.assertGreater(r.target, 102.0)
        self.assertIsNone(r.exit_reason)

    def test_overbought_and_trend_change_exits(self):
        s = snap(regime=an.STRONG_BULL, close=103.0, rsi=74, rsi_prev=77)
        self.assertEqual(review(pos(), s, GOLD, CFG).exit_reason, "RSI_OVERBOUGHT")
        self.assertEqual(review(pos(), snap(regime=an.STRONG_BEAR), GOLD, CFG).exit_reason, "TREND_CHANGE")

    def test_time_stop(self):
        s = snap(regime=an.STRONG_BULL, close=100.2)
        self.assertEqual(review(pos(sessions_held=30), s, GOLD, CFG).exit_reason, "TIME_STOP")

    def test_sudden_drop_threshold_uses_larger_of_pct_and_atr(self):
        lp, lh = sudden_drop_level(100.0, 104.0, 0.005, GOLD)
        self.assertAlmostEqual(lp, 97.5)                       # 2.5% > 2 x 0.5%
        self.assertAlmostEqual(lh, 101.4)
        lp2, _ = sudden_drop_level(100.0, 100.0, 0.03, SILVER)
        self.assertAlmostEqual(lp2, 94.0)                      # 2 x 3% ATR > 4%


class Ledger(unittest.TestCase):
    def setUp(self):
        self.l = EtfLedger(":memory:", 100000)

    def buy(self, d, qty, px, tranche=1, same=False):
        self.l.buy_fill(d, "GOLDBEES", qty, px, 10.0, tranche=tranche, kind="INITIAL", stop=95.0, target=105.0,
                        planned_value=60000, regime="X", reason="r", same_order=same)

    def test_average_tranches_and_fifo_sell(self):
        self.buy(D0, 100, 100.0)
        self.buy(D0 + timedelta(days=2), 50, 94.0, tranche=2)
        p = self.l.position("GOLDBEES")
        self.assertEqual((p["qty"], p["tranches"]), (150, 2))
        self.assertAlmostEqual(p["avg"], 98.0)
        self.assertEqual(len(self.l.tranches("GOLDBEES")), 2)
        net = self.l.sell(D0 + timedelta(days=5), "GOLDBEES", 150, 100.0, 15.0, "TARGET")
        self.assertAlmostEqual(net, 150 * 100 - 100 * 100 - 50 * 94 - 20 - 15)
        self.assertIsNone(self.l.position("GOLDBEES"))
        self.assertEqual(self.l.trades()[0]["tranches"], 2)

    def test_todays_units_not_sellable(self):
        self.buy(D0, 100, 100.0)
        self.buy(D0 + timedelta(days=1), 40, 99.0, tranche=2)
        self.assertEqual(self.l.sellable_qty("GOLDBEES", D0 + timedelta(days=1)), 100)
        self.l.sell(D0 + timedelta(days=1), "GOLDBEES", 140, 98.0, 5.0, "STOP")
        self.assertEqual(self.l.position("GOLDBEES")["qty"], 40)

    def test_sale_proceeds_settle_next_session(self):
        self.buy(D0, 100, 100.0)
        cash0 = self.l.getf("cash")
        self.l.sell(D0 + timedelta(days=1), "GOLDBEES", 100, 101.0, 10.0, "TARGET")
        self.assertEqual(self.l.available_cash(), cash0)
        self.l.settle(D0 + timedelta(days=1))
        self.assertEqual(self.l.available_cash(), cash0)
        self.l.settle(D0 + timedelta(days=2))
        self.assertAlmostEqual(self.l.available_cash(), cash0 + 100 * 101 - 10)

    def test_partial_fills_merge_into_one_tranche(self):
        self.buy(D0, 30, 100.0)
        self.buy(D0, 20, 101.0, same=True)
        self.assertEqual(len(self.l.tranches("GOLDBEES")), 1)
        p = self.l.position("GOLDBEES")
        self.assertEqual((p["qty"], p["tranches"]), (50, 1))
        self.assertAlmostEqual(p["avg"], 100.4)

    def test_duplicate_order_reference_rejected(self):
        self.assertTrue(self.l.add_order("EB1", D0, "GOLDBEES", "BUY", 1, 100.0))
        self.assertFalse(self.l.add_order("EB1", D0, "GOLDBEES", "BUY", 1, 100.0))


def _plan(led, d, qty=100, limit=100.0, stop=96.0, sym="GOLDBEES", tranche=1):
    led.add_order(buy_ref(sym, tranche, d), d, sym, "BUY", qty, limit, stop=stop, target=103.0, tranche=tranche,
                  kind="INITIAL", plan={"targets": {"preferred": 0.03}, "planned_value": 60000, "regime": "X",
                                        "atr_pct": 0.01})


class Engine(unittest.TestCase):
    def test_eod_is_idempotent_and_plans_expire(self):
        led = EtfLedger(":memory:", 100000)
        df = daily(drift=0.003, vol=0.006)
        d = df.index[-1]
        _plan(led, d - timedelta(days=3))
        end_of_day(d, led, {"GOLDBEES": df}, CFG)
        self.assertEqual(led.order(buy_ref("GOLDBEES", 1, d - timedelta(days=3)))["status"], "EXPIRED")
        _plan(led, d)
        res = end_of_day(d, led, {"GOLDBEES": df}, CFG)
        self.assertEqual(len([o for o in led.open_orders() if o["d"] == d.isoformat()]), 1)
        self.assertTrue(all(o["symbol"] != "GOLDBEES" for o in res.new_orders) or res.notes)

    def test_risk_block_stops_new_orders(self):
        led = EtfLedger(":memory:", 100000)
        led.set("peak_equity", 120000)
        df = daily()
        res = end_of_day(df.index[-1], led, {"GOLDBEES": df}, CFG)
        self.assertIn("drawdown", res.risk_block)
        self.assertEqual(res.new_orders, [])

    def test_missing_candle_is_a_data_issue(self):
        led = EtfLedger(":memory:", 100000)
        df = daily()
        res = end_of_day(df.index[-1] + timedelta(days=1), led, {"GOLDBEES": df}, CFG)
        self.assertIn("GOLDBEES", res.data_issues)


class Session(unittest.TestCase):
    def frames(self, bars):
        idx = [D0 + timedelta(days=i) for i in range(len(bars))]
        return {"GOLDBEES": pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx).assign(volume=1e6)}

    def test_planned_buy_fills_next_session_not_same(self):
        led = EtfLedger(":memory:", 100000)
        f = self.frames([(100, 101, 99, 100), (99.8, 100.5, 99.0, 100.2)])
        _plan(led, D0)
        simulate_session(D0, led, f, CFG)
        self.assertIsNone(led.position("GOLDBEES"))
        simulate_session(D0 + timedelta(days=1), led, f, CFG)
        p = led.position("GOLDBEES")
        self.assertAlmostEqual(p["avg"], 99.8 * (1 + GOLD.slippage_bps / 1e4))
        self.assertEqual(led.sellable_qty("GOLDBEES", D0 + timedelta(days=1)), 0)

    def test_gap_up_not_chased_gap_below_stop_cancelled(self):
        for open_, status in ((101.0, "EXPIRED"), (95.0, "CANCELLED")):
            led = EtfLedger(":memory:", 100000)
            _plan(led, D0)
            simulate_session(D0 + timedelta(days=1), led, self.frames([(100, 101, 99, 100), (open_, open_ + 1, open_ - 1, open_)]), CFG)
            self.assertEqual(led.order(buy_ref("GOLDBEES", 1, D0))["status"], status)

    def _held(self, bars, stop=96.0, target=104.0):
        led = EtfLedger(":memory:", 100000)
        led.buy_fill(D0, "GOLDBEES", 100, 100.0, 10, tranche=1, kind="INITIAL", stop=stop, target=target,
                     planned_value=60000, regime="X", reason="r")
        led.update_position("GOLDBEES", atr_pct=0.01)
        simulate_session(D0 + timedelta(days=1), led, self.frames(bars), CFG)
        return led

    def test_stop_gap_exits_at_open(self):
        t = self._held([(100, 101, 99, 100), (94, 95, 93, 94.5)]).trades()[0]
        self.assertEqual(t["reason"], "STOP_GAP")
        self.assertAlmostEqual(t["exit"], 94 * (1 - GOLD.slippage_bps / 1e4))

    def test_sudden_drop_exit_before_wide_stop(self):
        t = self._held([(100, 101, 99, 100), (99.8, 100, 96.5, 97)], stop=90.0).trades()[0]
        self.assertEqual(t["reason"], "SUDDEN_DROP")
        self.assertAlmostEqual(t["exit"], 97.5 * (1 - GOLD.slippage_bps / 1e4))

    def test_target(self):
        t = self._held([(100, 101, 99, 100), (101, 104.5, 100.5, 104)]).trades()[0]
        self.assertEqual(t["reason"], "TARGET")


class FakeMarket:
    def __init__(self, quotes, h1=None, m15=None, fail_candles=False):
        self.q, self.h1, self.m15, self.fail = quotes, h1, m15, fail_candles

    def quote(self, sym):
        if sym not in self.q:
            raise MarketDataError(f"{sym}: no quote")
        return self.q[sym]

    def candles(self, sym, interval, days):
        if self.fail:
            raise MarketDataError("down")
        return self.h1 if interval == "1h" else self.m15


def rising(n, start=100.0, step=0.05):
    c = start + step * np.arange(n)
    return pd.DataFrame({"open": c - 0.02, "high": c + 0.05, "low": c - 0.06, "close": c, "volume": 1e5},
                        index=pd.date_range("2026-11-01 09:15", periods=n, freq="15min"))


def falling(n, start=100.0, step=0.05):
    df = rising(n, start, -step)
    df["open"] = df["close"] + 0.03
    return df


NOW = datetime(2026, 11, 3, 11, 0, tzinfo=IST)       # Tuesday


class Cycle(unittest.TestCase):
    def held(self, d=D0, stop=96.0, target=104.0):
        led = EtfLedger(":memory:", 100000)
        led.buy_fill(d, "GOLDBEES", 100, 100.0, 10, tranche=1, kind="INITIAL", stop=stop, target=target,
                     planned_value=60000, regime="X", reason="r")
        led.update_position("GOLDBEES", atr_pct=0.01, last_close=100.0)
        return led

    def q(self, ltp, bid=None, ask=None, high=None):
        return Quote("GOLDBEES", ltp, bid or ltp - 0.01, ask or ltp + 0.01, None, high or ltp, None)

    def test_sudden_fall_from_previous_close_sells_and_alerts(self):
        led = self.held(stop=90.0)
        res = run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(97.3)}), PaperExecution(CFG), CFG, {"GOLDBEES": 100.0})
        self.assertIsNone(led.position("GOLDBEES"))
        self.assertEqual(led.trades()[0]["reason"], "SUDDEN_DROP")
        self.assertTrue(any("SUDDEN FALL" in a for a in res.alerts))
        self.assertTrue(led.get("cooldown_GOLDBEES"))
        self.assertIn("SUDDEN", format_cycle(res))

    def test_sudden_fall_from_days_high(self):
        led = self.held(stop=90.0)
        run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(101.0, high=104.0)}), PaperExecution(CFG), CFG,
                  {"GOLDBEES": 100.0})
        self.assertEqual(led.trades()[0]["reason"], "SUDDEN_DROP_FROM_HIGH")

    def test_quiet_market_does_nothing(self):
        led = self.held()
        res = run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(100.4)}), PaperExecution(CFG), CFG, {"GOLDBEES": 100.0})
        self.assertIsNotNone(led.position("GOLDBEES"))
        self.assertEqual(format_cycle(res), "")

    def test_units_bought_today_are_not_sold_today(self):
        led = self.held(d=NOW.date(), stop=99.0)
        run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(95.0)}), PaperExecution(CFG), CFG, {"GOLDBEES": 100.0})
        self.assertIsNotNone(led.position("GOLDBEES"))

    def test_no_quote_alerts_holding_unprotected(self):
        led = self.held()
        res = run_cycle(NOW, led, FakeMarket({}), PaperExecution(CFG), CFG, {"GOLDBEES": 100.0})
        self.assertTrue(any("NOT protected" in a for a in res.alerts))

    def test_outside_hours_and_weekend_skip(self):
        led = self.held()
        m = FakeMarket({"GOLDBEES": self.q(80.0)})
        self.assertIsNotNone(run_cycle(NOW.replace(hour=9, minute=16), led, m, PaperExecution(CFG), CFG, {}).skipped)
        self.assertIsNotNone(run_cycle(NOW + timedelta(days=4), led, m, PaperExecution(CFG), CFG, {}).skipped)
        self.assertIsNotNone(led.position("GOLDBEES"))

    def plan_led(self):
        led = EtfLedger(":memory:", 100000)
        _plan(led, D0, qty=50, limit=100.0, stop=97.0)
        return led

    def test_planned_buy_executes_with_confirmations(self):
        led = self.plan_led()
        res = run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(99.9)}, rising(40), rising(10)), PaperExecution(CFG),
                        CFG, {"GOLDBEES": 100.0})
        self.assertEqual(led.position("GOLDBEES")["qty"], 50, res.waiting)
        self.assertEqual(led.order(buy_ref("GOLDBEES", 1, D0))["status"], "FILLED")
        self.assertIn("1-hour", led.tranches("GOLDBEES")[0]["reason"])
        self.assertFalse(led.tranches("GOLDBEES")[0]["reason"].startswith(" |"))

    def test_buy_waits_on_each_failed_check(self):
        cases = {"ask": (self.q(100.2), rising(40), rising(10), False),
                 "spread": (Quote("GOLDBEES", 99.9, 99.5, 99.95), rising(40), rising(10), False),
                 "1h": (self.q(99.9), falling(40), rising(10), False),
                 "15m": (self.q(99.9), rising(40), falling(10), False),
                 "candles": (self.q(99.9), rising(40), rising(10), True)}
        for name, (q, h1, m15, fail) in cases.items():
            led = self.plan_led()
            res = run_cycle(NOW, led, FakeMarket({"GOLDBEES": q}, h1, m15, fail), PaperExecution(CFG), CFG, {"GOLDBEES": 100.0})
            self.assertIsNone(led.position("GOLDBEES"), name)
            self.assertIn("GOLDBEES", res.waiting, name)

    def test_buys_only_inside_window_and_not_with_kill_switch(self):
        m = FakeMarket({"GOLDBEES": self.q(99.9)}, rising(40), rising(10))
        for now, ks in ((NOW.replace(hour=9, minute=35), False), (NOW.replace(hour=15, minute=10), False), (NOW, True)):
            led = self.plan_led()
            run_cycle(now, led, m, PaperExecution(CFG), CFG, {"GOLDBEES": 100.0}, kill_switch=ks)
            self.assertIsNone(led.position("GOLDBEES"), (now, ks))

    def test_price_below_stop_cancels_plan(self):
        led = self.plan_led()
        run_cycle(NOW, led, FakeMarket({"GOLDBEES": self.q(96.5)}, rising(40), rising(10)), PaperExecution(CFG), CFG,
                  {"GOLDBEES": 100.0})
        self.assertEqual(led.order(buy_ref("GOLDBEES", 1, D0))["status"], "CANCELLED")

    def test_confirmation_helpers(self):
        self.assertTrue(hourly_confirms(rising(40))[0])
        self.assertFalse(hourly_confirms(falling(40))[0])
        self.assertFalse(hourly_confirms(rising(5))[0])
        self.assertTrue(m15_timing(rising(10))[0])
        self.assertFalse(m15_timing(falling(10))[0])


class Live(unittest.TestCase):
    def setUp(self):
        self.b = PaperBroker()
        self.led = EtfLedger(":memory:", 100000)
        self.ex = LiveExecution(self.b, CFG)
        _plan(self.led, D0, qty=50, limit=100.0, stop=97.0)
        self.o = self.led.order(buy_ref("GOLDBEES", 1, D0))
        self.q = Quote("GOLDBEES", 99.9, 99.88, 99.9)

    def run_buy(self):
        from app.etf.intraday import CycleResult
        res = CycleResult(NOW)
        self.ex.buy(self.led, self.o, self.q, NOW.date(), res)
        return res

    def test_books_only_what_the_broker_filled(self):
        self.b.market["GOLDBEES"] = 99.9
        self.b.partial_fill_qty = 20
        self.run_buy()
        self.assertEqual(self.led.position("GOLDBEES")["qty"], 20)
        self.assertEqual(self.led.order(self.o["ref"])["status"], "PARTIAL")
        self.b._orders[self.b._by_ref[self.o["ref"]]].filled_qty = 50
        self.b._orders[self.b._by_ref[self.o["ref"]]].status = "EXECUTED"
        from app.etf.intraday import CycleResult
        self.ex.sync(self.led, NOW.date(), CycleResult(NOW))
        p = self.led.position("GOLDBEES")
        self.assertEqual((p["qty"], p["tranches"]), (50, 1))
        self.assertEqual(self.led.order(self.o["ref"])["status"], "FILLED")

    def test_accepted_but_unfilled_is_not_a_position(self):
        self.b.market["GOLDBEES"] = 101.0
        self.run_buy()
        self.assertIsNone(self.led.position("GOLDBEES"))
        self.assertEqual(self.led.order(self.o["ref"])["status"], "OPEN")

    def test_no_duplicate_when_broker_already_has_the_reference(self):
        self.b.market["GOLDBEES"] = 101.0
        self.run_buy()
        self.run_buy()
        self.assertEqual(self.b.place_calls, 1)

    def test_timeout_reconciles_instead_of_retrying(self):
        self.b.market["GOLDBEES"] = 99.9
        self.b.fail_next_with_timeout = True
        res = self.run_buy()
        self.assertEqual(self.b.place_calls, 1)
        self.assertEqual(self.led.position("GOLDBEES")["qty"], 50)        # found by reference, booked from status
        self.assertTrue(any("unknown" in a for a in res.alerts))

    def test_funds_check(self):
        self.b.cash = 1000
        res = self.run_buy()
        self.assertEqual(self.b.place_calls, 0)
        self.assertIn("funds", res.waiting["GOLDBEES"])

    def test_sell_checks_holdings(self):
        from app.etf.intraday import CycleResult
        self.led.buy_fill(D0, "GOLDBEES", 50, 100.0, 5, tranche=1, kind="INITIAL", stop=97, target=103,
                          planned_value=1, regime="X", reason="r")
        res = CycleResult(NOW)
        self.ex.sell(self.led, "GOLDBEES", 50, self.q, "STOP", NOW.date(), res)
        self.assertEqual(self.b.place_calls, 0)                            # broker shows 0 units
        self.b.set_position("GOLDBEES", 50)
        self.b.market["GOLDBEES"] = 99.9
        self.ex.sell(self.led, "GOLDBEES", 50, self.q, "STOP", NOW.date(), res)
        self.assertIsNone(self.led.position("GOLDBEES"))


class Market(unittest.TestCase):
    def test_parse_quote_depth_and_fail_closed(self):
        q = parse_quote("GOLDBEES", {"last_price": 116.9, "depth": {"buy": [{"price": 116.88}], "sell": [{"price": 116.9}]},
                                     "ohlc": {"open": 117.5, "high": 117.8, "low": 116.7}})
        self.assertEqual((q.bid, q.ask, q.day_high), (116.88, 116.9, 117.8))
        self.assertLess(q.spread_bps, 3)
        with self.assertRaises(MarketDataError):
            parse_quote("GOLDBEES", {"last_price": None})

    def test_incomplete_candle_dropped(self):
        now = datetime(2026, 11, 3, 11, 10, tzinfo=IST)
        ts = [datetime(2026, 11, 3, 10, 45, tzinfo=IST).timestamp(), datetime(2026, 11, 3, 11, 0, tzinfo=IST).timestamp()]
        df = parse_candles({"candles": [[t, 1, 2, 0.5, 1.5, 10] for t in ts]}, 15, now)
        self.assertEqual(len(df), 1)


class Config(unittest.TestCase):
    def test_env_overrides_and_validation(self):
        c = load_etf_config({"ETF_CAPITAL": "50000", "ETF_SILVERBEES_MAX_LOSS_PCT": "0.5", "ETF_SYMBOLS": "goldbees",
                             "ETF_WINDOW_START": "10:00"})
        self.assertEqual((c.capital, c.profile("SILVERBEES").max_loss_pct, c.symbol_list, c.window_start),
                         (50000.0, 0.5, ["GOLDBEES"], time(10, 0)))
        with self.assertRaises(ValueError):
            load_etf_config({"ETF_SYMBOLS": "GOLDBEES,RELIANCE"})
        with self.assertRaises(ValueError):
            load_etf_config({"ETF_TRANCHE_WEIGHTS": "0.5,0.5,0.5"})

    def test_no_stt_on_gold_silver_etfs(self):
        self.assertEqual(CFG.costs.stt_pct, 0.0)

    def test_aggressive_mode_preset(self):
        c = load_etf_config({"ETF_STRATEGY_MODE": "aggressive"})
        self.assertEqual(c.strategy_mode, "aggressive")
        self.assertEqual(c.min_net_rr, 0.65)
        self.assertGreater(c.profile("GOLDBEES").max_exposure_pct, 80)


class Report(unittest.TestCase):
    def test_daily_report_has_required_fields(self):
        led = EtfLedger(":memory:", 100000)
        led.buy_fill(D0, "GOLDBEES", 100, 100.0, 10, tranche=1, kind="INITIAL", stop=96, target=103,
                     planned_value=60000, regime="X", reason="r")
        g, s = daily(drift=0.002, seed=4), daily(start=170, drift=0.002, seed=5)
        res = end_of_day(g.index[-1], led, {"GOLDBEES": g, "SILVERBEES": s}, CFG)
        txt = format_eod(res, led)
        for needle in ("GOLDBEES", "SILVERBEES", "regime", "RSI", "MACD", "ADX", "Signal:", "avg", "tranches",
                       "unrealized", "stop", "target", "available cash", "Realized", "Next:", "PAPER"):
            self.assertIn(needle, txt)


if __name__ == "__main__":
    unittest.main()
