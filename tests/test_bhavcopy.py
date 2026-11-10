import io, os, tempfile, unittest, urllib.error, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from app import run
from app.config import Settings
from app.data.groww_source import GrowwCandleSource, sync_daily
from app.data.nse_bhavcopy import (BhavcopyBlocked, BhavcopySource, BhavcopyUnavailable, MalformedBhavcopy,
                                   parse_bhavcopy, sync_bhavcopy)
from app.data.store import CandleStore
from app.paper.engine import INDEX_SYMBOL
from tests.synthetic import build
from tests.test_data_layer import FakeG

IST = ZoneInfo("Asia/Kolkata")
HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def row(d, sym, o, h, l, c, v, series="EQ"):
    return f"{d},{d},CM,NSE,STK,1,INE0,{sym},{series},,,,,{sym} LTD,{o},{h},{l},{c},{c},{c},,{c},,,{v},1.0,1,F1,1,,,,,"


def bhav(d, rows, header=HEADER):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv", "\n".join([header] + rows) + "\n")
    return buf.getvalue()


class FakeNSE:
    """Serves a bhavcopy for each day in `files`; any other day is a 404."""
    def __init__(self, files):
        self.files, self.asked = files, []

    def get(self, d):
        self.asked.append(d)
        return self.files.get(d)


def day_file(d, syms, px=100.0):
    return bhav(d, [row(d, s, px, px + 2, px - 2, px + 1, 1000) for s in syms])


class TestParse(unittest.TestCase):
    D = date(2026, 10, 28)

    def test_keeps_eq_rows_and_skips_other_series_and_bad_prices(self):
        skipped = []
        out = parse_bhavcopy(bhav(self.D, [row(self.D, "RELIANCE", 1194.5, 1204.3, 1156.4, 1158, 21866598),
                                           row(self.D, "ETERNAL", 330, 331.75, 323.8, 326, 21735164),
                                           row(self.D, "ETERNAL", 329, 329, 329, 329, 10800000, series="BL"),
                                           row(self.D, "M&M", 2850, 2893.4, 2826, 2869.9, 1387305),
                                           row(self.D, "BROKEN", "", 10, 9, 9.5, 5)]), self.D, skipped)
        self.assertEqual(out["RELIANCE"], ("2026-10-28", 1194.5, 1204.3, 1156.4, 1158.0, 21866598.0))
        self.assertEqual(out["ETERNAL"][4], 326.0)             # the block-deal (BL) row did not overwrite it
        self.assertIn("M&M", out)
        self.assertNotIn("BROKEN", out); self.assertEqual(skipped[0][0], "BROKEN")

    def test_wrong_day_missing_columns_and_non_zip_fail_loudly(self):
        with self.assertRaises(MalformedBhavcopy):
            parse_bhavcopy(bhav(self.D, [row(date(2026, 10, 27), "TCS", 1, 2, 1, 1, 1)]), self.D)
        with self.assertRaises(MalformedBhavcopy):
            parse_bhavcopy(bhav(self.D, [], header="TradDt,TckrSymb,ClsPric"), self.D)
        with self.assertRaises(MalformedBhavcopy):
            parse_bhavcopy(b"<html>blocked</html>", self.D)


class TestSource(unittest.TestCase):
    def opener(self, outcomes):
        calls = []

        def op(req, timeout):
            calls.append(req.full_url)
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            m = mock.MagicMock(); m.__enter__.return_value.read.return_value = o
            return m
        return op, calls

    def http(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, None)

    def test_404_means_no_file_403_stops_and_5xx_is_retried(self):
        d = date(2026, 10, 28)
        op, calls = self.opener([self.http(404)])
        self.assertIsNone(BhavcopySource(op, sleep=lambda s: None).get(d))
        self.assertIn("BhavCopy_NSE_CM_0_0_0_20261028_F_0000.csv.zip", calls[0])
        op, _ = self.opener([self.http(403)])
        with self.assertRaises(BhavcopyBlocked):
            BhavcopySource(op, sleep=lambda s: None).get(d)
        op, calls = self.opener([self.http(503), TimeoutError(), b"zip"])
        self.assertEqual(BhavcopySource(op, sleep=lambda s: None).get(d), b"zip")
        self.assertEqual(len(calls), 3)
        op, _ = self.opener([self.http(500)] * 4)
        with self.assertRaises(BhavcopyUnavailable):
            BhavcopySource(op, sleep=lambda s: None).get(d)


class TestSync(unittest.TestCase):
    DAYS = [date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8)]

    def setUp(self):
        self.st = CandleStore(tempfile.mktemp())
        for s in ("NSE-A", "NSE-B"):
            self.st.upsert(s, [("2025-10-03", 1, 2, 0.5, 1.5, 1)])
        self.st.upsert("NSE-OLD", [("2024-10-25", 1, 2, 0.5, 1.5, 1)])

    def sync(self, files, syms=("NSE-A", "NSE-B", "NSE-OLD", "NSE-NEW")):
        nse = FakeNSE(files)
        return sync_bhavcopy(self.st, nse, list(syms), self.DAYS, joinable_after=date(2025, 9, 24)), nse

    def test_fills_each_day_and_never_bridges_a_gap(self):
        out, _ = self.sync({d: day_file(d, ["A", "B", "OLD", "NEW"]) for d in self.DAYS})
        self.assertEqual(out["days"], [str(d) for d in self.DAYS]); self.assertIsNone(out["aborted"])
        self.assertEqual(self.st.last_date("NSE-A"), self.DAYS[-1])
        self.assertEqual(self.st.last_date("NSE-NEW"), self.DAYS[-1])          # newly listed: starts from NSE
        self.assertEqual(out["behind"], ["NSE-OLD"])                           # history stops a year early
        self.assertEqual(self.st.last_date("NSE-OLD"), date(2024, 10, 25))

    def test_latest_day_not_published_is_not_an_error_and_is_retried(self):
        files = {d: day_file(d, ["A", "B"]) for d in self.DAYS[:2]}
        out, _ = self.sync(files, syms=("NSE-A", "NSE-B"))
        self.assertEqual(out["not_published"], str(self.DAYS[-1])); self.assertEqual(out["errors"], {})
        files[self.DAYS[-1]] = day_file(self.DAYS[-1], ["A", "B"])
        out, nse = self.sync(files, syms=("NSE-A", "NSE-B"))
        self.assertEqual(nse.asked, [self.DAYS[-1]])                           # resumes, no re-downloads
        self.assertEqual(self.st.last_date("NSE-B"), self.DAYS[-1])

    def test_missing_older_day_stops_instead_of_leaving_a_hole(self):
        files = {self.DAYS[0]: day_file(self.DAYS[0], ["A", "B"]), self.DAYS[2]: day_file(self.DAYS[2], ["A", "B"])}
        out, nse = self.sync(files, syms=("NSE-A", "NSE-B"))
        self.assertTrue(out["aborted"]); self.assertIn(f"bhavcopy {self.DAYS[1]}", out["errors"])
        self.assertNotIn(self.DAYS[2], nse.asked)
        self.assertEqual(self.st.last_date("NSE-A"), self.DAYS[0])

    def test_symbol_absent_from_file_is_reported_not_invented(self):
        out, _ = self.sync({d: day_file(d, ["A"]) for d in self.DAYS}, syms=("NSE-A", "NSE-B"))
        self.assertEqual(len(out["absent"]["NSE-B"]), 3)
        self.assertEqual(self.st.last_date("NSE-B"), date(2025, 10, 3))


class TestGrowwCap(unittest.TestCase):
    def test_capped_stock_stops_at_cap_and_is_not_requested_once_stored_past_it(self):
        st, g = CandleStore(tempfile.mktemp()), FakeG()
        st.upsert("NSE-A", [("2025-10-01", 1, 2, 0.5, 1.5, 1)])
        st.upsert("NSE-DONE", [("2026-10-23", 1, 2, 0.5, 1.5, 1)])
        cap = date(2025, 10, 4)
        out = sync_daily(st, GrowwCandleSource(g, sleep=lambda s: None), ["NSE-NIFTY", "NSE-A", "NSE-DONE"],
                         datetime(2026, 10, 23, 17, 0, tzinfo=IST), end_caps={"NSE-A": cap, "NSE-DONE": cap})
        asked = {c["groww_symbol"]: c for c in g.calls}
        self.assertEqual(asked["NSE-A"]["end_time"][:10], "2025-10-04")
        self.assertEqual(asked["NSE-NIFTY"]["end_time"][:10], "2026-10-23")
        self.assertNotIn("NSE-DONE", asked); self.assertEqual(out["capped"], ["NSE-DONE"])


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, uni, self.days = build(self.tmp, n_days=300)
        self.p = [mock.patch.object(run, "MARKET_DB", f"{self.tmp}/market.db"), mock.patch.object(run, "UNIVERSE_CSV", uni),
                  mock.patch.object(run, "PAPER_DB", f"{self.tmp}/paper.db"), mock.patch.object(run, "REPORTS", Path(self.tmp) / "r"),
                  mock.patch.object(run, "EVENTS_CSV", f"{self.tmp}/none.csv"), mock.patch.object(run, "FETCH_LOCK", f"{self.tmp}/l")]
        for x in self.p: x.start()
        self.addCleanup(lambda: [x.stop() for x in self.p])

    def at(self, d, h=19):
        return datetime(d.year, d.month, d.day, h, 0, tzinfo=IST)

    def drop_stocks(self, d, keep=0):
        self.store.con.execute("DELETE FROM candles WHERE d=? AND symbol!=? AND symbol NOT IN "
                               "(SELECT symbol FROM candles WHERE d=? AND symbol!=? ORDER BY symbol LIMIT ?)",
                               (str(d), INDEX_SYMBOL, str(d), INDEX_SYMBOL, keep))
        self.store.con.commit()

    def test_day_without_stock_candles_waits_instead_of_being_used_up(self):
        from app.paper.ledger import Ledger
        run.catch_up(Settings(), 250000, None, self.at(self.days[-3]))
        self.drop_stocks(self.days[-1], keep=30)                                # 30 of 50 < 80%
        run.catch_up(Settings(), 250000, None, self.at(self.days[-1]))
        self.assertEqual(Ledger(run.PAPER_DB, 1).get("last_processed"), str(self.days[-2]))
        build(self.tmp, n_days=300)                                             # the stock candles arrive
        self.assertTrue(run._stocks_ready(CandleStore(f"{self.tmp}/market.db"), self.days[-1]))
        run.catch_up(Settings(), 250000, None, self.at(self.days[-1]))
        self.assertEqual(Ledger(run.PAPER_DB, 1).get("last_processed"), str(self.days[-1]))

    def test_fetch_in_bhavcopy_mode_caps_groww_stocks_and_loads_nse(self):
        seen = {}

        def fake_sync(store, src, syms, now, end_caps=None):
            seen["caps"] = end_caps
            return {"updated": {INDEX_SYMBOL: 1}, "errors": {}, "aborted": None, "skipped": [], "dropped": {}, "capped": []}
        fake_b = {"days": ["2026-10-28"], "rows": 50, "absent": {}, "behind": [], "dropped": {}, "requests": 1,
                  "errors": {"bhavcopy 2026-10-27": "x"}, "aborted": "NSE bhavcopy download stopped", "not_published": None}
        with mock.patch.dict(os.environ, {"STOCK_SOURCE": "bhavcopy", "GROWW_EQUITY_UNTIL": "2025-10-04"}), \
             mock.patch.object(run, "_held_symbols", lambda: set()), \
             mock.patch("app.data.groww_source.groww_client_from_env", lambda: object()), \
             mock.patch("app.data.groww_source.GrowwCandleSource", lambda g, **kw: object()), \
             mock.patch("app.data.groww_source.sync_daily", fake_sync), \
             mock.patch.object(run, "_sync_bhavcopy", lambda store, stocks, now: seen.setdefault("stocks", stocks) and fake_b):
            s = run._fetch()
        self.assertNotIn(INDEX_SYMBOL, seen["caps"]); self.assertEqual(len(seen["caps"]), 52)  # 50 stocks + 2 ETFs
        self.assertIn("NSE-GOLDBEES", seen["caps"]); self.assertIn("NSE-SILVERBEES", seen["stocks"])
        self.assertEqual(set(seen["caps"].values()), {date(2025, 10, 4)})
        self.assertIn("bhavcopy 2026-10-27", s["errors"]); self.assertTrue(s["aborted"])  # reaches the Telegram alert

    def test_daily_retries_while_stock_candles_are_missing_and_says_so(self):
        d = self.days[-1]
        self.drop_stocks(d)
        sent, fetches = [], []
        n = mock.Mock(); n.send = lambda t: sent.append(t) or True; n.alert = n.send

        class FixedDT(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(d.year, d.month, d.day, 19, 0, tzinfo=IST)
        with mock.patch.dict(os.environ, {"STOCK_SOURCE": "bhavcopy", "DAILY_RETRIES": "2", "DAILY_RETRY_WAIT": "0",
                                          "HEARTBEAT_URL": ""}), \
             mock.patch.object(run, "_fetch", lambda: fetches.append(1) or {"errors": {}, "updated": {}}), \
             mock.patch.object(run, "catch_up", lambda *a, **k: 0), mock.patch.object(run, "datetime", FixedDT), \
             mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: n)):
            self.assertEqual(run.cmd_daily(None), 0)
        self.assertEqual(len(fetches), 3)
        self.assertTrue(any("Stock candles for" in t for t in sent))


if __name__ == "__main__":
    unittest.main()
