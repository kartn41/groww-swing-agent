import io, json, logging, tempfile, unittest, urllib.error
from datetime import date
from unittest import mock
from app.notify import TelegramNotifier, chunk_text, find_chat_ids, format_daily, format_intraday_session
from app.paper.engine import DayResult
from app.paper.ledger import Ledger

TOKEN, CHAT = "123456:ABC-SecretTokenValue", "987654"


class Resp:
    def __init__(self, body): self._b = body
    def read(self): return self._b.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeNet:
    """Scripted responses: each item is a body string, an HTTP code (int) or an Exception."""
    def __init__(self, script): self.script, self.requests = list(script), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        item = self.script.pop(0) if self.script else '{"ok": true}'
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            body = json.dumps({"ok": False, "parameters": {"retry_after": 3}}) if item == 429 else "{}"
            raise urllib.error.HTTPError(req.full_url, item, "err", {}, io.BytesIO(body.encode()))
        return Resp(item)


def notifier(script, sleeps=None):
    net = FakeNet(script)
    return TelegramNotifier(TOKEN, CHAT, opener=net, sleep=(sleeps.append if sleeps is not None else lambda s: None)), net


class TestSend(unittest.TestCase):
    def test_success_posts_to_right_chat(self):
        n, net = notifier(['{"ok": true}'])
        self.assertTrue(n.send("hello"))
        body = net.requests[0].data.decode()
        self.assertIn("chat_id=987654", body); self.assertIn("text=hello", body)
        self.assertIn("/sendMessage", net.requests[0].full_url)

    def test_never_raises_on_any_failure(self):
        for script in ([OSError("down")] * 3, [ValueError("x")] * 3, ["not json"] * 3, [500] * 3):
            n, _ = notifier(script)
            self.assertFalse(n.send("hello"))

    def test_retries_429_using_retry_after_then_succeeds(self):
        sleeps = []
        n, net = notifier([429, '{"ok": true}'], sleeps)
        self.assertTrue(n.send("hi")); self.assertEqual(len(net.requests), 2); self.assertEqual(sleeps, [3.0])

    def test_permanent_errors_are_not_retried(self):
        for code in (400, 401, 403):
            n, net = notifier([code])
            self.assertFalse(n.send("hi")); self.assertEqual(len(net.requests), 1, code)

    def test_retries_are_bounded(self):
        n, net = notifier([OSError("down")] * 10)
        n.send("hi"); self.assertEqual(len(net.requests), 3)

    def test_long_message_is_chunked_in_order(self):
        n, net = notifier([])
        text = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200))
        self.assertTrue(n.send(text)); self.assertGreater(len(net.requests), 1)
        sent = "\n".join(__import__("urllib.parse").parse.parse_qs(r.data.decode())["text"][0] for r in net.requests)
        self.assertEqual(sent, text)
        for r in net.requests:
            self.assertLessEqual(len(__import__("urllib.parse").parse.parse_qs(r.data.decode())["text"][0]), 4000)

    def test_chunk_text_handles_single_huge_line(self):
        parts = chunk_text("y" * 9000)
        self.assertEqual("".join(parts), "y" * 9000); self.assertTrue(all(len(p) <= 4000 for p in parts))

    def test_secrets_are_redacted_before_sending(self):
        n, net = notifier([])
        with mock.patch.dict("os.environ", {"GROWW_TOTP_SECRET": "GROWWSECRET99"}):
            n.send(f"leak test {TOKEN} and GROWWSECRET99")
        sent = net.requests[0].data.decode()
        self.assertNotIn("GROWWSECRET99", sent); self.assertNotIn("SecretTokenValue", sent)

    def test_token_never_logged_on_network_error(self):
        stream = io.StringIO(); h = logging.StreamHandler(stream)
        lg = logging.getLogger("app.notify"); lg.addHandler(h)
        try:
            n, _ = notifier([OSError(f"failed https://api.telegram.org/bot{TOKEN}/sendMessage")] * 3)
            n.send("x")
        finally:
            lg.removeHandler(h)
        self.assertNotIn(TOKEN, stream.getvalue())


class TestSetup(unittest.TestCase):
    def test_from_env(self):
        self.assertIsNone(TelegramNotifier.from_env({}))
        self.assertIsNone(TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": ""}))
        self.assertIsNotNone(TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}))

    def test_find_chat_ids(self):
        body = json.dumps({"ok": True, "result": [
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "Karthi"}}},
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "Karthi"}}},
            {"message": {"chat": {"id": -222, "type": "group", "title": "Team"}}}]})
        chats = find_chat_ids("tok", opener=FakeNet([body]))
        self.assertEqual(sorted(c["id"] for c in chats), [-222, 111])
        self.assertEqual(find_chat_ids("tok", opener=FakeNet(['{"ok": true, "result": []}'])), [])


class TestFormat(unittest.TestCase):
    def test_daily_message_labels_plans_as_plans_and_marks_paper(self):
        led = Ledger(tempfile.mktemp(), 250000)
        res = DayResult(date(2026, 10, 23), regime="WEAK_UP", equity=251000, cash=200000,
                        new_orders=[{"symbol": "INFY", "qty": 10, "entry": 1500.0, "stop": 1440.0,
                                     "target": 1650.0, "score": 71.2}],
                        fills=["BUY 5 TCS @ 4000.00"])
        text = format_daily(res, led)
        self.assertIn("PAPER", text); self.assertIn("no real orders", text)
        self.assertIn("PLANNED for next session (not placed yet)", text)
        self.assertIn("Fills today", text)
        self.assertIn("+0.40%", text)

    def test_capital_split_and_trade_percentages(self):
        led = Ledger(tempfile.mktemp(), 250000)
        order = lambda s: {"symbol": s, "sector": "IT", "qty": 10, "stop": 90.0, "target": 130.0,
                           "strategy": "pullback", "regime": "STRONG_UP"}
        led.open_position(date(2026, 10, 1), order("AAA"), 100.0, 0.0)
        led.close_position(date(2026, 10, 20), led.positions()[0], 110.0, "TARGET", 0.0)     # +Rs100, +10%
        led.open_position(date(2026, 10, 5), order("BBB"), 200.0, 0.0)
        led.close_position(date(2026, 10, 23), led.positions()[0], 190.0, "STOP", 0.0)       # -Rs100, -5%
        led.open_position(date(2026, 10, 21), order("CCC"), 1000.0, 0.0)
        led.mark("CCC", 1100.0)
        res = DayResult(date(2026, 10, 23), regime="STRONG_UP", equity=50000, cash=39000)
        text = format_daily(res, led)
        self.assertIn("Capital used: Rs11,000 invested (22.0%), cash 78.0%", text)
        self.assertIn("CCC 10 @ 1000.00, close 1100.00 (+10.00%, Rs+1,000), value Rs11,000 = 22.0% of equity", text)
        self.assertIn("Sold today (after costs):\n- BBB: 10 bought 200.00 on 2026-10-05, sold 190.00 [STOP], "
                      "net Rs-100 (-5.00%), held 18d", text)
        self.assertNotIn("- AAA: 10 bought", text)
        self.assertIn("Closed trades so far: 2 (1 won, 1 lost, win rate 50%), net Rs+0", text)
        self.assertIn("Average win +10.00%, best AAA +10.00%", text)
        self.assertIn("Average loss -5.00%, worst BBB -5.00%", text)

    def test_skipped_day(self):
        led = Ledger(tempfile.mktemp(), 250000)
        self.assertIn("no trading session", format_daily(DayResult(date(2026, 10, 24), skipped=True), led))

    def test_no_orders_message(self):
        led = Ledger(tempfile.mktemp(), 250000)
        self.assertIn("NO TRADE", format_daily(DayResult(date(2026, 10, 23), equity=250000, cash=250000), led))

    def test_intraday_session_message(self):
        from app.intraday.engine import SessionResult

        led = Ledger(tempfile.mktemp(), 250000)
        res = SessionResult(date(2026, 10, 23), equity=250500, cash=248000, fills=["TEST long 10 @ 100.00"])
        text = format_intraday_session(res, led)
        self.assertIn("PAPER intraday", text)
        self.assertIn("no real orders", text)
        self.assertIn("Fills this session", text)


if __name__ == "__main__":
    unittest.main()
