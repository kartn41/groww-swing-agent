import io, os, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock
import app.run as run
from app.config import Mode, Settings
from tests.synthetic import build


class TestRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        store, uni, cls.days = build(cls.tmp, n_days=300)
        cls.patches = [mock.patch.object(run, "MARKET_DB", f"{cls.tmp}/market.db"),
                       mock.patch.object(run, "UNIVERSE_CSV", uni),
                       mock.patch.object(run, "DATA_DIR", Path(cls.tmp)),
                       mock.patch.object(run, "PAPER_DB", f"{cls.tmp}/paper.db"),
                       mock.patch.object(run, "REPORTS", Path(cls.tmp) / "reports"),
                       mock.patch.object(run, "setup_logging", lambda *a, **k: None),
                       mock.patch.object(run, "load_env_file", lambda *a, **k: {"loaded": [], "warnings": []})]
        for p in cls.patches: p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches: p.stop()

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_as_of_runs_on_scratch_account(self):
        rc, out, _ = self.call("paper", "--as-of", self.days[-1].isoformat())
        self.assertEqual(rc, 0); self.assertIn("PAPER report", out)
        self.assertFalse(os.path.exists(f"{self.tmp}/paper.db"))       # real paper account untouched

    def test_as_of_weekend_reports_closed(self):
        sat = next(d for d in (self.days[-1].fromordinal(self.days[-1].toordinal() + i) for i in range(1, 7))
                   if d.weekday() == 5)
        rc, out, _ = self.call("paper", "--as-of", sat.isoformat())
        self.assertEqual(rc, 0); self.assertIn("market closed", out)

    def test_replay_prints_summary(self):
        rng = f"{self.days[-20].isoformat()}:{self.days[-1].isoformat()}"
        rc, out, _ = self.call("paper", "--replay", rng)
        self.assertEqual(rc, 0); self.assertIn("Days processed: 20", out)

    def test_refuses_in_live_mode(self):
        with mock.patch.object(run, "load_settings", lambda: Settings(mode=Mode.LIVE)):
            rc, _, err = self.call("paper", "--as-of", self.days[-1].isoformat())
        self.assertEqual(rc, 2); self.assertIn("LIVE", err)

    def test_today_sends_daily_telegram_and_alerts_on_failure(self):
        sent = []
        class Fake:
            def send(self, t): sent.append(("send", t)); return True
            def alert(self, t): sent.append(("alert", t)); return True
        fake_now = mock.Mock(); 
        import datetime as dt
        d = self.days[-1]
        class FixedDT(dt.datetime):
            @classmethod
            def now(cls, tz=None): return cls(d.year, d.month, d.day, 17, 0, tzinfo=tz)
        with mock.patch.object(run, "datetime", FixedDT), \
             mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: Fake())):
            rc, out, _ = self.call("paper", "--today")
            self.assertEqual(rc, 0)
            self.assertEqual([k for k, _ in sent], ["send"])
            self.assertIn("PAPER daily report", sent[0][1])
            sent.clear()
            with mock.patch("app.paper.engine._process", side_effect=RuntimeError("boom")):
                os.remove(f"{self.tmp}/paper.db")
                rc, _, _ = self.call("paper", "--today")
            self.assertEqual(rc, 1)
            self.assertEqual([k for k, _ in sent], ["alert"]); self.assertIn("FAILED", sent[0][1])

    def test_scratch_runs_never_send_telegram(self):
        boom = mock.Mock(side_effect=AssertionError("must not construct a notifier"))
        with mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(boom)):
            rc, _, _ = self.call("paper", "--as-of", self.days[-1].isoformat())
            rc2, _, _ = self.call("paper", "--replay", f"{self.days[-5]}:{self.days[-1]}")
        self.assertEqual((rc, rc2), (0, 0)); boom.assert_not_called()

    def test_telegram_test_command_without_config(self):
        with mock.patch.object(run.TelegramNotifier, "from_env", staticmethod(lambda *a, **k: None)):
            rc, _, err = self.call("telegram-test")
        self.assertEqual(rc, 2); self.assertIn("missing", err)

    def test_backtest_command_writes_report(self):
        with mock.patch.object(run, "EVENTS_CSV", f"{self.tmp}/none.csv"):
            rc, out, _ = self.call("backtest", "--start", self.days[-40].isoformat(),
                                   "--holdout", self.days[-15].isoformat(), "--fold-months", "1", "--quick")
        self.assertEqual(rc, 0)
        path = out.split("report: ")[1].split("\n")[0].strip()
        text = open(path).read()
        for section in ("Development period", "Untouched holdout period", "Walk-forward", "Red flags", "survivorship"):
            self.assertIn(section, text)

    def test_status_without_account(self):
        rc, out, _ = self.call("status")
        self.assertEqual(rc, 0); self.assertIn("No paper account", out)


if __name__ == "__main__":
    unittest.main()
