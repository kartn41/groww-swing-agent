import base64, os, subprocess, tempfile, threading, unittest, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import app.run as run
from app.config import Settings
from app.dashboard import DashConfig, make_server
from tests.synthetic import build

IST = ZoneInfo("Asia/Kolkata")
PW = "correct-horse-battery"


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        from app.paper.engine import process_day
        from app.paper.ledger import Ledger
        from app.universe import load_universe
        store, uni_path, days = build(cls.tmp, n_days=300)
        led = Ledger(f"{cls.tmp}/paper.db", 250000)
        for d in days[-25:]:
            process_day(d, led, store, load_universe(uni_path)[0], Settings())
        led.con.close()
        os.makedirs(f"{cls.tmp}/logs"); Path(f"{cls.tmp}/logs/agent.log").write_text("token=SECRETSECRET99 ok\n")
        cls.ks = f"{cls.tmp}/KILL"
        cls.cfg = DashConfig(f"{cls.tmp}/paper.db", f"{cls.tmp}/market.db", f"{cls.tmp}/reports",
                             f"{cls.tmp}/logs/agent.log", Settings(kill_switch_file=cls.ks), PW, port=0)
        cls.srv = make_server(cls.cfg)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, pw=PW, data=None):
        r = urllib.request.Request(self.base + path, data=data)
        if pw is not None:
            r.add_header("Authorization", "Basic " + base64.b64encode(f"me:{pw}".encode()).decode())
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_requires_password(self):
        self.assertEqual(self.req("/", pw=None)[0], 401)
        self.assertEqual(self.req("/", pw="wrong-password")[0], 401)

    def test_binds_localhost_only_and_refuses_weak_password(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        with self.assertRaises(RuntimeError):
            make_server(DashConfig("a", "b", "c", "d", Settings(), "short", port=0))

    def test_overview_shows_mode_and_sections(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        for text in ("PAPER mode", "New entries allowed", "Open positions", "Risk limits in force", "Stop new entries"):
            self.assertIn(text, body)

    def test_kill_switch_needs_token_then_toggles(self):
        self.assertEqual(self.req("/kill", data=b"token=forged")[0], 403)
        self.assertFalse(os.path.exists(self.ks))
        token = self.req("/")[1].split("name='token' value='")[1].split("'")[0]
        self.req("/kill", data=f"token={token}".encode())
        self.assertTrue(os.path.exists(self.ks))
        self.assertIn("New entries stopped", self.req("/")[1])
        self.req("/kill", data=f"token={token}".encode())
        self.assertFalse(os.path.exists(self.ks))

    def test_logs_are_redacted(self):
        with mock.patch.dict(os.environ, {"X_TOKEN": "SECRETSECRET99"}):
            body = self.req("/logs")[1]
        self.assertNotIn("SECRETSECRET99", body)

    def test_no_order_endpoints(self):
        for path in ("/order", "/place", "/buy"):
            self.assertEqual(self.req(path, data=b"x=1")[0], 404)


class TestScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        store, uni, cls.days = build(cls.tmp, n_days=300)
        cls.p = [mock.patch.object(run, "MARKET_DB", f"{cls.tmp}/market.db"), mock.patch.object(run, "UNIVERSE_CSV", uni),
                 mock.patch.object(run, "PAPER_DB", f"{cls.tmp}/paper.db"), mock.patch.object(run, "REPORTS", Path(cls.tmp) / "r"),
                 mock.patch.object(run, "EVENTS_CSV", f"{cls.tmp}/none.csv")]
        for x in cls.p: x.start()

    @classmethod
    def tearDownClass(cls):
        for x in cls.p: x.stop()

    def at(self, d, h=17):
        return datetime(d.year, d.month, d.day, h, 0, tzinfo=IST)

    def test_first_run_processes_latest_day_only_then_catches_up_missed_days(self):
        if os.path.exists(run.PAPER_DB): os.remove(run.PAPER_DB)
        run.catch_up(Settings(), 250000, None, self.at(self.days[-6]))
        from app.paper.ledger import Ledger
        self.assertEqual(Ledger(run.PAPER_DB, 1).get("last_processed"), str(self.days[-6]))
        sent = []
        n = mock.Mock(); n.send = lambda t: sent.append(t) or True
        run.catch_up(Settings(), 250000, n, self.at(self.days[-1]))         # server was down 5 days
        self.assertEqual(Ledger(run.PAPER_DB, 1).get("last_processed"), str(self.days[-1]))
        self.assertEqual(len(sent), 1); self.assertIn("Caught up 5 missed trading days", sent[0])
        self.assertEqual(run.catch_up(Settings(), 250000, None, self.at(self.days[-1])), 0)   # nothing left

    def test_before_close_does_not_process_today(self):
        if os.path.exists(run.PAPER_DB): os.remove(run.PAPER_DB)
        run.catch_up(Settings(), 250000, None, self.at(self.days[-1], h=11))
        from app.paper.ledger import Ledger
        self.assertEqual(Ledger(run.PAPER_DB, 1).get("last_processed"), str(self.days[-2]))

    def test_units_render_without_placeholders(self):
        dest = tempfile.mkdtemp()
        subprocess.run(["bash", "deploy/install_systemd.sh"], check=True, capture_output=True,
                       env={**os.environ, "DEST": dest, "NO_SYSTEMCTL": "1", "AGENT_DIR": "/home/trader/gsa"})
        files = sorted(os.listdir(dest))
        self.assertEqual(files, ["groww-agent-daily.service", "groww-agent-daily.timer", "groww-agent-dashboard.service",
                                 "groww-agent-etf-monitor.service", "groww-agent-etf-monitor.timer"])
        self.assertNotIn("groww-agent-monitor.timer", files)          # the LIVE GTT monitor stays opt-in
        for f in files:
            t = Path(dest, f).read_text()
            self.assertNotIn("__", t)
        svc = Path(dest, "groww-agent-daily.service").read_text()
        self.assertIn("User=trader", svc); self.assertIn("/home/trader/gsa/.venv/bin/python -m app.run daily", svc)
        tmr = Path(dest, "groww-agent-daily.timer").read_text()
        self.assertIn("Mon..Fri", tmr); self.assertIn("Asia/Kolkata", tmr); self.assertIn("Persistent=true", tmr)

    def test_live_monitor_units_only_on_request(self):
        dest = tempfile.mkdtemp()
        subprocess.run(["bash", "deploy/install_systemd.sh", "--with-live-monitor"], check=True, capture_output=True,
                       env={**os.environ, "DEST": dest, "NO_SYSTEMCTL": "1"})
        self.assertIn("groww-agent-monitor.timer", os.listdir(dest))


if __name__ == "__main__":
    unittest.main()
