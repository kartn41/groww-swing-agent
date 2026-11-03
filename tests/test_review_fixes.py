"""Regression tests for every defect found in review #1 (A-K)."""
import os, subprocess, sys, tempfile, types, unittest
from datetime import date

from app.config import Settings, RiskLimits, CostModel, Mode, PROJECT_ROOT
from app.broker.base import BrokerTimeout, BrokerUnavailable
from app.broker.paper import PaperBroker
from app.execution.executor import Executor, reconcile
from app.execution.state_machine import S, TradeLifecycle
from app.risk.engine import TradePlan, PortfolioState, evaluate
from app.risk.costs import order_charges

LIM, COST, DAY = RiskLimits(), CostModel(), date(2026, 10, 20)
P = TradePlan("INFY", "IT", 1000.0, 960.0, 1100.0)


def life():
    t = TradeLifecycle("x"); t.to(S.AWAITING_CONFIRMATION); t.to(S.APPROVED); return t


class Base(unittest.TestCase):
    def setUp(self):
        self.ks = os.path.join(tempfile.mkdtemp(), "KILL")
        self.b = PaperBroker(); self.b.market["INFY"] = 999.0
        self.ex = Executor(self.b, Settings(kill_switch_file=self.ks))
        self.d = evaluate(P, PortfolioState(500_000, 500_000), LIM, COST)

    def go(self, price=999.0, t=None):
        t = t or life()
        return self.ex.submit_entry(P, self.d, "bo", t, DAY, price), t


class A_LookupOutageNoDuplicate(Base):
    def test_outage_before_placing_blocks_order(self):
        self.b.lookup_unavailable = True
        r, t = self.go()
        self.assertFalse(r.submitted); self.assertEqual(self.b.place_calls, 0)
        self.assertIs(t.state, S.CANCELLED)

    def test_one_signal_one_order_even_across_outage(self):
        self.go()
        self.b.lookup_unavailable = True
        self.go()
        self.assertEqual(self.b.place_calls, 1)


class B_StopProtection(Base):
    def test_fill_places_stop_for_filled_qty(self):
        r, t = self.go()
        self.assertIs(t.state, S.OPEN)
        stops = list(self.b.stops().values())
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][1], self.d.qty); self.assertEqual(stops[0][2], P.stop)
        self.assertFalse(r.unprotected)

    def test_partial_fill_protects_only_filled(self):
        self.b.partial_fill_qty = 7
        r, t = self.go()
        self.assertIs(t.state, S.PARTIALLY_FILLED)
        self.assertEqual(list(self.b.stops().values())[0][1], 7)

    def test_stop_failure_raises_alert_flag(self):
        self.b.stop_fails = True
        r, t = self.go()
        self.assertIs(t.state, S.OPEN)              # shares exist; state must say so
        self.assertTrue(r.unprotected); self.assertIn("ALERT", r.reason)

    def test_unfilled_order_places_no_stop(self):
        self.b.market["INFY"] = 1003.0               # within chase tolerance, above limit => no fill
        r, t = self.go(price=1003.0)
        self.assertIs(t.state, S.ORDER_SUBMITTED); self.assertEqual(self.b.stops(), {})


class C_KillSwitchIndependentOfCwd(unittest.TestCase):
    def test_default_path_is_absolute_inside_project(self):
        s = Settings()
        self.assertTrue(os.path.isabs(s.kill_switch_file))
        self.assertTrue(s.kill_switch_file.startswith(str(PROJECT_ROOT)))

    def test_kill_switch_works_from_root_cwd(self):
        code = ("import os,sys;sys.path.insert(0,%r);os.chdir('/');"
                "from app.config import Settings;from app.execution.executor import kill_switch_active;"
                "print(kill_switch_active(Settings()))") % str(PROJECT_ROOT)
        ks = Settings().kill_switch_file
        os.makedirs(os.path.dirname(ks), exist_ok=True)
        open(ks, "w").close()
        try:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
        finally:
            os.remove(ks)
        self.assertEqual(out, "True")


class D_UnknownOutcome(Base):
    def test_accepted_then_timeout_reconciles_no_retry(self):
        self.b.fail_next_with_timeout = True
        r, t = self.go()
        self.assertEqual(self.b.place_calls, 1); self.assertIs(t.state, S.OPEN)

    def test_timeout_and_not_visible_means_reconcile_not_retry(self):
        self.b.raise_before_accept = BrokerTimeout("conn reset")
        r, t = self.go()
        self.assertEqual(self.b.place_calls, 1)
        self.assertIs(t.state, S.RECONCILIATION_REQUIRED)

    def test_timeout_and_lookup_down_means_reconcile(self):
        class B2(PaperBroker):
            n = 0
            def get_order_by_reference(self, ref):
                B2.n += 1
                if B2.n > 1: raise BrokerUnavailable("down")
                return None
        b = B2(); b.market["INFY"] = 999.0; b.fail_next_with_timeout = True
        t = life()
        Executor(b, Settings(kill_switch_file=self.ks)).submit_entry(P, self.d, "bo", t, DAY, 999.0)
        self.assertIs(t.state, S.RECONCILIATION_REQUIRED)


class E_StatusMapping(Base):
    def _with(self, status, fill=None):
        self.b.force_status = status
        if fill is not None: self.b.partial_fill_qty = fill
        return self.go()[1].state

    def test_failed_is_cancelled(self):
        self.b.market["INFY"] = 1003.0
        self.assertIs(self._with_price("FAILED"), S.CANCELLED)

    def _with_price(self, status):
        self.b.force_status = status
        return self.go(price=1003.0)[1].state

    def test_completed_and_delivery_awaited_are_filled(self):
        for st in ("COMPLETED", "DELIVERY_AWAITED", "EXECUTED"):
            self.setUp(); self.assertIs(self._with(st), S.OPEN, st)

    def test_pending_statuses_stay_submitted(self):
        for st in ("NEW", "ACKED", "APPROVED", "TRIGGER_PENDING"):
            self.setUp(); self.b.market["INFY"] = 1003.0
            self.assertIs(self._with_price(st), S.ORDER_SUBMITTED, st)

    def test_unknown_status_goes_to_reconcile(self):
        self.b.market["INFY"] = 1003.0
        self.assertIs(self._with_price("SOMETHING_NEW"), S.RECONCILIATION_REQUIRED)


class F_ReconcileOutage(unittest.TestCase):
    def test_outage_is_never_reported_as_in_sync(self):
        class Down(PaperBroker):
            def positions(self): raise BrokerUnavailable("down")
        out = reconcile({"INFY": 10}, Down())
        self.assertEqual(len(out), 1); self.assertIn("UNVERIFIED", out[0])


class H_CashCoversCharges(unittest.TestCase):
    def test_order_plus_buy_charges_within_cash(self):
        lim = RiskLimits(risk_per_trade_pct=5, max_open_risk_pct=10, max_position_alloc_pct=100,
                         max_sector_alloc_pct=100, min_reward_risk=0.5)
        d = evaluate(TradePlan("X", "S", 100.0, 95.0, 120.0), PortfolioState(1_000_000, 10_015), lim, COST)
        # 100 shares @ 100.10 = 10,010 fits the cash but not cash + ~Rs23 buy charges
        self.assertTrue(d.approved, d.reasons)
        self.assertEqual(d.qty, 99)
        fill = d.qty * 100.0 * (1 + lim.slippage_bps / 10_000)
        self.assertLessEqual(fill + order_charges(fill, "BUY", COST), 10_015)


class I_FreshPriceNoChase(Base):
    def test_gap_above_tolerance_not_chased(self):
        r, t = self.go(price=1010.0)
        self.assertFalse(r.submitted); self.assertEqual(self.b.place_calls, 0)

    def test_missing_price_fails_closed(self):
        r, _ = self.go(price=None)
        self.assertFalse(r.submitted); self.assertEqual(self.b.place_calls, 0)

    def test_price_below_stop_invalidates(self):
        r, _ = self.go(price=955.0)
        self.assertFalse(r.submitted)


class J_MarkToMarketLoss(unittest.TestCase):
    def test_unrealized_loss_counts_toward_daily_limit(self):
        d = evaluate(P, PortfolioState(500_000, 500_000, unrealized_pnl_today=-8_000), LIM, COST)
        self.assertFalse(d.approved)
        self.assertTrue(any("daily loss" in r for r in d.reasons))


# ---------------- Groww adapter against a fake SDK (no network) ----------------
class FakeGroww:
    VALIDITY_DAY = "DAY"; EXCHANGE_NSE = "NSE"; SEGMENT_CASH = "CASH"; PRODUCT_CNC = "CNC"
    ORDER_TYPE_LIMIT = "LIMIT"; ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_STOP_LOSS = "SL"; ORDER_TYPE_STOP_LOSS_MARKET = "SL_M"
    TRANSACTION_TYPE_BUY = "BUY"; TRANSACTION_TYPE_SELL = "SELL"
    SMART_ORDER_TYPE_GTT = "GTT"; TRIGGER_DIRECTION_DOWN = "DOWN"
    last = None

    def __init__(self, token):
        FakeGroww.last = self
        self.orders, self.calls, self.place_exc, self.list_exc = [], [], None, None
        self.holdings, self.positions = [], []

    def place_order(self, **kw):
        self.calls.append(("place", kw))
        if self.place_exc: raise self.place_exc
        return {"groww_order_id": "G1", "order_status": "ACKED", "order_reference_id": kw["order_reference_id"]}

    def get_order_list(self, segment, page, page_size):
        if self.list_exc: raise self.list_exc
        return {"order_list": self.orders[page * page_size:(page + 1) * page_size]}

    def get_holdings_for_user(self, timeout=5): return {"holdings": self.holdings}
    def get_positions_for_user(self, segment): return {"positions": self.positions}

    def create_smart_order(self, **kw):
        self.calls.append(("gtt", kw)); return {"smart_order_id": "gtt_1"}


class GrowwAdapterTests(unittest.TestCase):
    def setUp(self):
        sys.modules["growwapi"] = types.SimpleNamespace(GrowwAPI=FakeGroww)
        from app.broker.groww_adapter import GrowwBroker
        self.gb = GrowwBroker(Settings(mode=Mode.LIVE), "tok")
        self.g = FakeGroww.last

    def tearDown(self):
        sys.modules.pop("growwapi", None)

    def test_product_and_segment_are_hardwired(self):
        from app.broker.base import OrderRequest
        self.gb.place_order(OrderRequest("INFY", 1, "BUY", "LIMIT", 1000.0, None, "SW26102012345678"))
        kw = self.g.calls[0][1]
        self.assertEqual((kw["product"], kw["segment"], kw["exchange"]), ("CNC", "CASH", "NSE"))

    def test_sl_order_type_maps_to_real_constant(self):
        from app.broker.base import OrderRequest
        self.gb.place_order(OrderRequest("INFY", 1, "SELL", "SL", 950.0, 955.0, "SW26102012345678"))
        self.assertEqual(self.g.calls[0][1]["order_type"], "SL")

    def test_any_sdk_error_on_place_is_unknown_outcome(self):
        from app.broker.base import OrderRequest
        self.g.place_exc = ConnectionResetError("reset")
        with self.assertRaises(BrokerTimeout):
            self.gb.place_order(OrderRequest("INFY", 1, "BUY", "LIMIT", 1000.0, None, "SW26102012345678"))

    def test_lookup_paginates_and_finds(self):
        self.g.orders = [{"groww_order_id": f"G{i}", "order_reference_id": f"R{i:08d}",
                          "order_status": "EXECUTED", "filled_quantity": 3} for i in range(60)]
        st = self.gb.get_order_by_reference("R00000055")
        self.assertEqual((st.broker_order_id, st.filled_qty), ("G55", 3))

    def test_lookup_definitive_not_found(self):
        self.g.orders = [{"groww_order_id": "G1", "order_reference_id": "OTHER123", "order_status": "NEW"}]
        self.assertIsNone(self.gb.get_order_by_reference("MISSING1"))

    def test_lookup_error_is_unavailable_not_none(self):
        self.g.list_exc = RuntimeError("503")
        with self.assertRaises(BrokerUnavailable):
            self.gb.get_order_by_reference("MISSING1")

    def test_positions_include_same_day_buys(self):
        self.g.holdings = [{"trading_symbol": "TCS", "quantity": 5}]
        self.g.positions = [{"trading_symbol": "INFY", "quantity": 7}]
        self.assertEqual(self.gb.positions(), {"TCS": 5, "INFY": 7})

    def test_unexpected_payload_is_unavailable(self):
        self.g.positions = [{"symbol": "INFY"}]           # schema drift
        with self.assertRaises(BrokerUnavailable):
            self.gb.positions()

    def test_gtt_stop_is_cnc_sell_down(self):
        self.gb.place_stop_protection("INFY", 10, 960.0, "SL26102012345678")
        kw = self.g.calls[0][1]
        self.assertEqual((kw["product_type"], kw["trigger_direction"], kw["order"]["transaction_type"]),
                         ("CNC", "DOWN", "SELL"))
        self.assertLess(float(kw["order"]["price"]), 960.0)


if __name__ == "__main__":
    unittest.main()
