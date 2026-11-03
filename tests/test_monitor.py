import sys, tempfile, types, unittest
from datetime import date
from app.broker.base import BrokerUnavailable, OrderStatus
from app.broker.paper import PaperBroker
from app.execution.monitor import LiveRegistry, Monitor

DAY = date(2026, 10, 26)


class Setup:
    def make(self, qty=10, held=10):
        self.b = PaperBroker()
        self.reg = LiveRegistry(tempfile.mktemp())
        sid = self.b.place_stop_protection("INFY", qty, 1440.0, "SL26102600000001")
        self.b.set_position("INFY", held)
        self.reg.register_entry("INFY", qty, 1440.0, sid, "pullback", "SW26102600000001", DAY)
        self.sid, self.mon = sid, Monitor(self.b, self.reg)
        return self.mon.cycle(DAY)


class TestMonitor(unittest.TestCase, Setup):
    def test_healthy_position_untouched(self):
        r = self.make()
        self.assertEqual((r.actions, r.alerts), ([], [])); self.assertEqual(self.b.stop_calls, [])

    def test_positions_outage_changes_nothing(self):
        self.make()
        def down(): raise BrokerUnavailable("503")
        self.b.positions = down
        r = self.mon.cycle(DAY)
        self.assertTrue(r.aborted); self.assertEqual(self.b.stop_calls, [])

    def test_gtt_status_outage_changes_nothing_for_that_symbol(self):
        self.make()
        self.b.set_position("INFY", 4); self.b.stops_unavailable = True
        r = self.mon.cycle(DAY)
        self.assertEqual(self.b.stop_calls, []); self.assertTrue(any("unavailable" in a for a in r.alerts))

    def test_shares_gone_cancels_active_gtt_and_closes(self):
        self.make()
        self.b.set_position("INFY", 0)
        r = self.mon.cycle(DAY)
        self.assertIn(("cancel", self.sid), self.b.stop_calls)
        self.assertEqual(self.reg.open_positions(), [])
        self.assertEqual(self.mon.cycle(DAY).actions, [])           # idempotent next cycle

    def test_partial_sale_shrinks_gtt(self):
        self.make()
        self.b.set_position("INFY", 6)
        r = self.mon.cycle(DAY)
        self.assertIn(("modify", self.sid, 6, 1440.0), self.b.stop_calls)
        self.assertTrue(any("expected 10" in a for a in r.alerts))

    def test_extra_shares_alert_but_protect_only_agent_quantity(self):
        self.make()
        self.b.set_position("INFY", 15)
        r = self.mon.cycle(DAY)
        self.assertEqual(self.b.stop_calls, []); self.assertTrue(any("RECONCILE" in a for a in r.alerts))

    def test_entry_fill_growth_resizes_gtt(self):
        self.make(qty=4, held=4)
        self.b.set_position("INFY", 10)
        self.b.get_order_by_reference = lambda ref: OrderStatus("G1", ref, "EXECUTED", 10)
        r = self.mon.cycle(DAY)
        self.assertIn(("modify", self.sid, 10, 1440.0), self.b.stop_calls)
        self.assertEqual(self.reg.open_positions()[0]["qty"], 10)

    def test_cancelled_or_missing_gtt_is_rearmed(self):
        self.make()
        self.b.stop_status[self.sid] = "CANCELLED"
        r = self.mon.cycle(DAY)
        new_id = self.reg.open_positions()[0]["stop_id"]
        self.assertNotEqual(new_id, self.sid); self.assertEqual(self.b.get_stop(new_id).status, "ACTIVE")
        self.assertTrue(any("re-armed" in a for a in r.alerts))

    def test_rearm_failure_is_critical_alert(self):
        self.make()
        self.b.stop_status[self.sid] = "EXPIRED"; self.b.stop_fails = True
        r = self.mon.cycle(DAY)
        self.assertTrue(any(a.startswith("CRITICAL") and "UNPROTECTED" in a for a in r.alerts))

    def test_triggered_gtt_left_alone(self):
        self.make()
        self.b.stop_status[self.sid] = "TRIGGERED"
        r = self.mon.cycle(DAY)
        self.assertEqual(self.b.stop_calls, []); self.assertTrue(any("in progress" in a for a in r.actions))

    def test_stop_price_change_is_applied(self):
        self.make()
        self.reg.update("INFY", stop=1480.0); self.reg.con.commit()
        self.mon.cycle(DAY)
        self.assertIn(("modify", self.sid, 10, 1480.0), self.b.stop_calls)

    def test_orphan_agent_gtt_cancelled_but_user_gtts_never_touched(self):
        self.make()
        user_gtt = self.b.place_stop_protection("TCS", 5, 3000.0, "USERMADE0001")   # not in registry
        old = self.b.place_stop_protection("INFY", 10, 1400.0, "SL26101000000009")
        self.reg.add_stop(old, "INFY"); self.reg.con.commit()                         # agent's superseded GTT
        r = self.mon.cycle(DAY)
        self.assertIn(("cancel", old), self.b.stop_calls)
        self.assertNotIn(("cancel", user_gtt), self.b.stop_calls)
        self.assertNotIn(("cancel", self.sid), self.b.stop_calls)


class FakeGroww:
    SEGMENT_CASH, SMART_ORDER_TYPE_GTT, TRIGGER_DIRECTION_DOWN = "CASH", "GTT", "DOWN"
    ORDER_TYPE_LIMIT, TRANSACTION_TYPE_SELL, TRANSACTION_TYPE_BUY = "LIMIT", "SELL", "BUY"
    ORDER_TYPE_MARKET, ORDER_TYPE_STOP_LOSS, ORDER_TYPE_STOP_LOSS_MARKET = "MARKET", "SL", "SL_M"
    last = None

    def __init__(self, token):
        FakeGroww.last = self; self.calls = []; self.fail = False

    def get_smart_order(self, **kw):
        self.calls.append(("get", kw))
        if self.fail: raise RuntimeError("503")
        return {"smart_order_id": kw["smart_order_id"], "status": "ACTIVE", "trading_symbol": "INFY",
                "quantity": 10, "trigger_price": "1440.00"}

    def modify_smart_order(self, **kw):
        self.calls.append(("modify", kw)); return {"smart_order_id": kw["smart_order_id"], "status": "ACTIVE", "quantity": kw["quantity"]}

    def cancel_smart_order(self, **kw):
        self.calls.append(("cancel", kw)); return {"smart_order_id": kw["smart_order_id"], "status": "CANCELLED"}


class TestGrowwGttCalls(unittest.TestCase):
    def setUp(self):
        sys.modules["growwapi"] = types.SimpleNamespace(GrowwAPI=FakeGroww)
        from app.broker.groww_adapter import GrowwBroker
        from app.config import Mode, Settings
        self.gb = GrowwBroker(Settings(mode=Mode.LIVE), "tok"); self.g = FakeGroww.last

    def tearDown(self):
        sys.modules.pop("growwapi", None)

    def test_documented_parameters(self):
        st = self.gb.get_stop("gtt_1")
        self.assertEqual((st.qty, st.trigger, st.status), (10, 1440.0, "ACTIVE"))
        self.gb.modify_stop("gtt_1", "INFY", 6, 1450.0)
        kw = self.g.calls[-1][1]
        self.assertEqual((kw["smart_order_type"], kw["segment"], kw["quantity"], kw["trigger_price"],
                          kw["trigger_direction"], kw["order"]["transaction_type"]), ("GTT", "CASH", 6, "1450.00", "DOWN", "SELL"))
        self.assertLess(float(kw["order"]["price"]), 1450.0)
        self.gb.cancel_stop("gtt_1")
        self.assertEqual(self.g.calls[-1][1], {"segment": "CASH", "smart_order_type": "GTT", "smart_order_id": "gtt_1"})

    def test_lookup_failure_is_unavailable(self):
        self.g.fail = True
        with self.assertRaises(BrokerUnavailable):
            self.gb.get_stop("gtt_1")


if __name__ == "__main__":
    unittest.main()
