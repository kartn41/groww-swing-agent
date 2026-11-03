import os, tempfile, unittest
from datetime import date

from app.config import Mode, Settings, RiskLimits, CostModel, load_settings, ARM_PHRASE
from app.risk.costs import order_charges, round_trip_cost
from app.risk.engine import TradePlan, PortfolioState, OpenPosition, evaluate
from app.broker.paper import PaperBroker
from app.broker.groww_adapter import GrowwBroker, LiveModeRequired
from app.execution.executor import Executor, make_reference_id, reconcile
from app.execution.state_machine import S, TradeLifecycle, IllegalTransition

LIM, COST = RiskLimits(), CostModel()


def plan(entry=1000.0, stop=960.0, target=1100.0, sym="INFY", sector="IT"):
    return TradePlan(sym, sector, entry, stop, target)


def pf(**kw):
    base = dict(equity=500_000.0, cash=500_000.0)
    base.update(kw)
    return PortfolioState(**base)


class TestMode(unittest.TestCase):
    def test_default_is_paper(self):
        self.assertIs(load_settings({}).mode, Mode.PAPER)

    def test_live_env_without_arm_file_is_paper(self):
        self.assertIs(load_settings({"GROWW_MODE": "LIVE", "LIVE_ARM_FILE": "/nonexistent"}).mode, Mode.PAPER)

    def test_live_needs_exact_phrase(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("yes"); p = f.name
        self.assertIs(load_settings({"GROWW_MODE": "LIVE", "LIVE_ARM_FILE": p}).mode, Mode.PAPER)
        with open(p, "w") as f: f.write(ARM_PHRASE)
        self.assertIs(load_settings({"GROWW_MODE": "LIVE", "LIVE_ARM_FILE": p}).mode, Mode.LIVE)
        os.unlink(p)

    def test_groww_broker_refuses_in_paper_mode(self):
        with self.assertRaises(LiveModeRequired):
            GrowwBroker(Settings(mode=Mode.PAPER), "token")


class TestCosts(unittest.TestCase):
    def test_buy_has_stamp_no_dp_sell_has_dp_no_stamp(self):
        b, s = order_charges(100_000, "BUY", COST), order_charges(100_000, "SELL", COST)
        self.assertGreater(b, 100)            # STT alone is Rs100
        self.assertNotEqual(round(b, 2), round(s, 2))

    def test_zero_value(self):
        self.assertEqual(order_charges(0, "BUY", COST), 0.0)

    def test_round_trip_is_sum(self):
        rt = round_trip_cost(1000, 1100, 100, COST)
        self.assertAlmostEqual(rt, order_charges(100_000, "BUY", COST) + order_charges(110_000, "SELL", COST))


class TestSizing(unittest.TestCase):
    def test_risk_budget_respected(self):
        d = evaluate(plan(), pf(), LIM, COST)
        self.assertTrue(d.approved, d.reasons)
        # 0.5% of 5L = 2500 budget; per-share risk ~ 40 + slippage => ~60 shares
        self.assertLessEqual(d.qty * (1000 * 1.001 - 960 * 0.999), 2500 + 1e-6)
        self.assertGreater(d.qty, 0)

    def test_reject_low_reward_risk(self):
        d = evaluate(plan(target=1050.0), pf(), LIM, COST)     # RR ~1.25
        self.assertFalse(d.approved)
        self.assertTrue(any("reward/risk" in r for r in d.reasons))

    def test_costs_can_flip_a_borderline_trade(self):
        # gross RR exactly 2.0 must fail once costs + slippage are counted
        d = evaluate(plan(entry=1000, stop=950, target=1100), pf(), LIM, COST)
        self.assertFalse(d.approved)

    def test_zero_qty_rejected(self):
        d = evaluate(plan(), pf(cash=500.0), LIM, COST)
        self.assertFalse(d.approved)

    def test_invalid_plan_rejected(self):
        self.assertFalse(evaluate(plan(stop=1010.0), pf(), LIM, COST).approved)

    def test_position_alloc_cap(self):
        d = evaluate(plan(entry=100, stop=99, target=103), pf(), LIM, COST)
        self.assertLessEqual(d.qty * 100, 500_000 * 0.20 + 100)

    def test_sector_cap(self):
        held = [OpenPosition("TCS", "IT", 100, 1700.0, 1650.0)]   # 1.7L in IT = 34%
        d = evaluate(plan(), pf(positions=held), LIM, COST)
        room = 500_000 * 0.35 - 170_000                     # Rs 5,000 of IT room left
        self.assertGreater(evaluate(plan(), pf(), LIM, COST).qty, room / 1000)  # cap really binds
        self.assertFalse(d.approved and d.qty * 1000 > room)

    def test_max_positions(self):
        held = [OpenPosition(f"S{i}", f"X{i}", 1, 100.0, 90.0) for i in range(3)]
        d = evaluate(plan(), pf(positions=held), LIM, COST)
        self.assertFalse(d.approved)

    def test_no_averaging_down(self):
        held = [OpenPosition("INFY", "IT", 10, 1100.0, 1000.0)]
        self.assertFalse(evaluate(plan(), pf(positions=held), LIM, COST).approved)

    def test_aggregate_open_risk_cap(self):
        held = [OpenPosition("A", "S1", 100, 1000.0, 900.0)]       # risk 10,000 > 2% of 5L
        self.assertFalse(evaluate(plan(), pf(positions=held), LIM, COST).approved)

    def test_daily_loss_limit_blocks(self):
        d = evaluate(plan(), pf(realized_pnl_today=-8000.0), LIM, COST)   # 1.6% loss
        self.assertFalse(d.approved)

    def test_weekly_and_max_drawdown_block(self):
        self.assertFalse(evaluate(plan(), pf(week_start_equity=530_000.0), LIM, COST).approved)
        self.assertFalse(evaluate(plan(), pf(peak_equity=600_000.0), LIM, COST).approved)

    def test_stale_data_fails_closed(self):
        self.assertFalse(evaluate(plan(), pf(data_ok=False), LIM, COST).approved)

    def test_kill_switch_flag_blocks(self):
        self.assertFalse(evaluate(plan(), pf(entries_disabled=True), LIM, COST).approved)

    def test_liquidity_cap_binds(self):
        uncapped = evaluate(plan(), pf(), LIM, COST)
        d = evaluate(plan(), pf(), LIM, COST, avg_daily_value=5_000_000.0)  # 1% = Rs50k
        self.assertTrue(d.approved, d.reasons)
        self.assertLess(d.qty, uncapped.qty)
        self.assertLessEqual(d.qty * 1000, 50_000)

    def test_tiny_orders_rejected_because_fixed_costs_dominate(self):
        d = evaluate(plan(), pf(), LIM, COST, avg_daily_value=500_000.0)    # cap => ~4 shares
        self.assertFalse(d.approved)
        self.assertTrue(any("reward/risk" in r for r in d.reasons))


class TestStateMachine(unittest.TestCase):
    def test_legal_path_recorded(self):
        t = TradeLifecycle("t1")
        for s in (S.AWAITING_CONFIRMATION, S.APPROVED, S.ORDER_SUBMITTED, S.OPEN, S.EXIT_PENDING, S.CLOSED):
            t.to(s)
        self.assertEqual(len(t.history), 6)
        self.assertIs(t.state, S.CLOSED)

    def test_illegal_transition(self):
        with self.assertRaises(IllegalTransition):
            TradeLifecycle("t2").to(S.OPEN)

    def test_terminal_states_are_terminal(self):
        t = TradeLifecycle("t3"); t.to(S.CANCELLED)
        with self.assertRaises(IllegalTransition):
            t.to(S.APPROVED)


def approved_life():
    t = TradeLifecycle("x"); t.to(S.AWAITING_CONFIRMATION); t.to(S.APPROVED); return t


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(kill_switch_file=os.path.join(self.tmp, "KILL"))
        self.broker = PaperBroker()
        self.ex = Executor(self.broker, self.settings)
        self.p = plan()
        self.dec = evaluate(self.p, pf(), LIM, COST)
        self.day = date(2026, 10, 20)

    def test_reference_id_format_and_determinism(self):
        r = make_reference_id("INFY", "breakout", self.day)
        self.assertTrue(8 <= len(r) <= 20 and r.isalnum())
        self.assertEqual(r, make_reference_id("INFY", "breakout", self.day))
        self.assertNotEqual(r, make_reference_id("TCS", "breakout", self.day))

    def test_fill_marks_open(self):
        self.broker.market["INFY"] = 999.0
        life = approved_life()
        res = self.ex.submit_entry(self.p, self.dec, "breakout", life, self.day, 999.0)
        self.assertTrue(res.submitted)
        self.assertIs(life.state, S.OPEN)

    def test_accepted_but_unfilled_is_not_open(self):
        self.broker.market["INFY"] = 1200.0           # limit 1000 not reached
        life = approved_life()
        self.ex.submit_entry(self.p, self.dec, "breakout", life, self.day, 999.0)
        self.assertIs(life.state, S.ORDER_SUBMITTED)  # never assume a fill

    def test_duplicate_signal_does_not_double_submit(self):
        self.broker.market["INFY"] = 999.0
        self.ex.submit_entry(self.p, self.dec, "breakout", approved_life(), self.day, 999.0)
        res = self.ex.submit_entry(self.p, self.dec, "breakout", approved_life(), self.day, 999.0)
        self.assertFalse(res.submitted)
        self.assertEqual(self.broker.place_calls, 1)

    def test_timeout_reconciles_instead_of_retrying(self):
        self.broker.market["INFY"] = 999.0
        self.broker.fail_next_with_timeout = True
        life = approved_life()
        res = self.ex.submit_entry(self.p, self.dec, "breakout", life, self.day, 999.0)
        self.assertEqual(self.broker.place_calls, 1)   # no second placement
        self.assertTrue(res.submitted)
        self.assertIs(life.state, S.OPEN)

    def test_kill_switch_blocks_orders(self):
        open(self.settings.kill_switch_file, "w").close()
        life = approved_life()
        res = self.ex.submit_entry(self.p, self.dec, "breakout", life, self.day, 999.0)
        self.assertFalse(res.submitted)
        self.assertEqual(self.broker.place_calls, 0)

    def test_rejected_decision_never_reaches_broker(self):
        bad = evaluate(plan(target=1010.0), pf(), LIM, COST)
        res = self.ex.submit_entry(self.p, bad, "breakout", approved_life(), self.day, 999.0)
        self.assertFalse(res.submitted)
        self.assertEqual(self.broker.place_calls, 0)

    def test_reconcile_detects_mismatch(self):
        self.broker.market["INFY"] = 999.0
        self.ex.submit_entry(self.p, self.dec, "breakout", approved_life(), self.day, 999.0)
        self.assertEqual(reconcile({"INFY": self.dec.qty}, self.broker), [])
        self.assertEqual(len(reconcile({"INFY": self.dec.qty + 5}, self.broker)), 1)
        self.assertEqual(len(reconcile({}, self.broker)), 1)


if __name__ == "__main__":
    unittest.main()
