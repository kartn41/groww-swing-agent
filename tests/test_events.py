import os, tempfile, unittest
from datetime import date
from app.config import Settings
from app.events import EventsError, load_events
from app.paper.engine import process_day
from app.paper.ledger import Ledger


def write(text, age_days=0):
    p = os.path.join(tempfile.mkdtemp(), "events.csv")
    with open(p, "w") as f:
        f.write(text)
    if age_days:
        t = os.path.getmtime(p) - age_days * 86400
        os.utime(p, (t, t))
    return p


class TestLoad(unittest.TestCase):
    def test_nse_style_columns_and_formats(self):
        cal = load_events(write("SYMBOL,COMPANY,PURPOSE,DATE\nINFY,Infosys,Financial Results,23-Oct-2026\n"
                                "TCS,TCS,Dividend,2026-10-24\nHDFC,HDFC,Bonus,25/10/2026\nX,X,Results,not a date\n"))
        self.assertTrue(cal.usable)
        self.assertTrue(cal.blocking_event("INFY", date(2026, 10, 20), 7))
        self.assertIsNone(cal.blocking_event("TCS", date(2026, 10, 20), 7))      # dividend: warn only
        self.assertEqual(len(cal.upcoming("TCS", date(2026, 10, 20), 7)), 1)
        self.assertTrue(cal.blocking_event("HDFC", date(2026, 10, 20), 7))
        self.assertIsNone(cal.blocking_event("INFY", date(2026, 10, 24), 7))     # already past
        self.assertTrue(any("unreadable" in w for w in cal.warnings))

    def test_missing_stale_and_bad(self):
        self.assertFalse(load_events("/nonexistent/events.csv").usable)
        self.assertFalse(load_events(write("Symbol,Date\nA,2026-10-24\n", age_days=30)).usable)
        with self.assertRaises(EventsError):
            load_events(write("Name,When\nA,2026-10-24\n"))

    def test_unknown_purpose_is_treated_as_blocking(self):
        cal = load_events(write("Symbol,Date\nA,2026-10-24\n"))
        self.assertTrue(cal.blocking_event("A", date(2026, 10, 22), 7))


class TestEngineEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_strategy_paper import market
        cls.store, cls.uni, cls.days, _ = market()

    def order_day(self):
        """A day on which the engine normally plans orders (so 'no orders' can't be true by accident)."""
        for d in reversed(self.days[-70:]):
            r = process_day(d, Ledger(tempfile.mktemp(), 250000), self.store, self.uni, Settings())
            if r.new_orders:
                return d, r
        self.fail("synthetic market has no order day")

    def test_blocking_event_rejects_the_planned_symbol(self):
        d, normal = self.order_day()
        sym = normal.new_orders[0]["symbol"]
        cal = load_events(write(f"Symbol,Purpose,Date\n{sym},Financial Results,{d.isoformat()}\n"))
        r = process_day(d, Ledger(tempfile.mktemp(), 250000), self.store, self.uni, Settings(), events=cal)
        self.assertNotIn(sym, [o["symbol"] for o in r.new_orders])
        self.assertTrue(r.rejections[sym].startswith("event risk"))

    def test_required_calendar_missing_blocks_all_new_orders(self):
        d, normal = self.order_day()
        r = process_day(d, Ledger(tempfile.mktemp(), 250000), self.store, self.uni, Settings(),
                        events=load_events("/nonexistent.csv"), events_required=True)
        self.assertEqual(r.new_orders, [])
        self.assertTrue(any("required: no new entries" in n for n in r.notes))
        r2 = process_day(d, Ledger(tempfile.mktemp(), 250000), self.store, self.uni, Settings(),
                         events=load_events("/nonexistent.csv"), events_required=False)
        self.assertTrue(r2.new_orders)                      # paper default: warn, still trade
        self.assertTrue(all(any("calendar unavailable" in e for e in o["evidence"]) for o in r2.new_orders))


if __name__ == "__main__":
    unittest.main()
