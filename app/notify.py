"""Telegram notifications (send-only). Uses the standard library; no extra package.

Design rules:
 - A notification failure must NEVER break trading: send() never raises.
 - The bot only SENDS. It never reads or acts on incoming messages, so a leaked bot token
   cannot be used to command the agent.
 - Everything is passed through secret redaction before leaving the machine.
 - Messages are always labelled PAPER or LIVE; proposed orders are never worded as fills.
"""
from __future__ import annotations
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Callable, Optional

from app.logging_setup import redact, secret_values

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000                      # Telegram's hard limit is 4096


def chunk_text(text: str, limit: int = MAX_LEN) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                      # a single very long line
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, opener: Callable = urllib.request.urlopen,
                 sleep: Callable = time.sleep, timeout: float = 10.0, max_attempts: int = 3) -> None:
        self._token, self.chat_id = token, str(chat_id)
        self._open, self._sleep, self._timeout, self._attempts = opener, sleep, timeout, max_attempts

    @classmethod
    def from_env(cls, environ=None, **kw) -> Optional["TelegramNotifier"]:
        env = os.environ if environ is None else environ
        tok, chat = (env.get("TELEGRAM_BOT_TOKEN") or "").strip(), (env.get("TELEGRAM_CHAT_ID") or "").strip()
        return cls(tok, chat, **kw) if tok and chat else None

    def _post_once(self, text: str) -> tuple[bool, bool, float]:
        """Returns (delivered, retryable, retry_after_seconds)."""
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(API.format(token=self._token, method="sendMessage"), data=data, method="POST")
        try:
            with self._open(req, timeout=self._timeout) as r:
                body = json.loads(r.read().decode())
            return bool(body.get("ok")), False, 0.0
        except urllib.error.HTTPError as e:
            log.warning("telegram HTTP %s", e.code)
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                return False, True, wait
            return False, e.code >= 500, 1.0      # 400/401/403/404: permanent, do not retry

    def send(self, text: str) -> bool:
        """Best effort. Never raises. True only if every chunk was delivered."""
        try:
            text = redact(text, secret_values() + [self._token])
            for part in chunk_text(text):
                delivered = False
                for _ in range(self._attempts):
                    try:
                        ok, retryable, wait = self._post_once(part)
                    except Exception as e:                      # network error: log the type only
                        log.warning("telegram send error: %s", type(e).__name__)
                        ok, retryable, wait = False, True, 1.0
                    if ok:
                        delivered = True
                        break
                    if not retryable:
                        break
                    self._sleep(min(wait, 30.0))
                if not delivered:
                    return False
            return True
        except Exception as e:                                  # absolutely never propagate
            log.warning("telegram notifier failure: %s", type(e).__name__)
            return False

    def alert(self, text: str) -> bool:
        return self.send("ALERT\n" + text)


def find_chat_ids(token: str, opener: Callable = urllib.request.urlopen, timeout: float = 10.0) -> list[dict]:
    """Helper for first-time setup: lists chats that have messaged the bot."""
    req = urllib.request.Request(API.format(token=token, method="getUpdates"))
    with opener(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    seen: dict[int, dict] = {}
    for u in body.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                "name": chat.get("username") or chat.get("first_name") or chat.get("title")}
    return list(seen.values())


def _share(part: float, whole: float) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def _trade_pct(t) -> float:
    cost = t["entry"] * t["qty"]
    return t["net"] / cost if cost else 0.0


def _trade_line(t) -> str:
    held = (date.fromisoformat(t["exit_date"]) - date.fromisoformat(t["entry_date"])).days
    return (f"{t['qty']} bought {t['entry']:.2f} on {t['entry_date']}, sold {t['exit']:.2f} [{t['reason']}], "
            f"net Rs{t['net']:+,.0f} ({_trade_pct(t):+.2%}), held {held}d")


def _closed_stats(trades, start: float) -> list[str]:
    wins = [_trade_pct(t) for t in trades if t["net"] > 0]
    losses = [_trade_pct(t) for t in trades if t["net"] <= 0]
    total = sum(t["net"] for t in trades)
    best = max(trades, key=_trade_pct)
    worst = min(trades, key=_trade_pct)
    L = [f"Closed trades so far: {len(trades)} ({len(wins)} won, {len(losses)} lost, "
         f"win rate {len(wins) / len(trades):.0%}), net Rs{total:+,.0f} ({total / start:+.2%} of start capital)"]
    if wins:
        L.append(f"- Average win {sum(wins) / len(wins):+.2%}, best {best['symbol']} {_trade_pct(best):+.2%}")
    if losses:
        L.append(f"- Average loss {sum(losses) / len(losses):+.2%}, worst {worst['symbol']} {_trade_pct(worst):+.2%}")
    return L


def format_daily(res, ledger, mode: str = "PAPER") -> str:
    """Compact daily message. Planned orders are labelled as plans, never as fills."""
    if res.skipped:
        return f"{mode} {res.d}: no trading session / no data.\n" + "\n".join(res.notes)
    start = ledger.getf("start_capital")
    invested = res.equity - res.cash
    L = [f"{mode} daily report {res.d}" + (" (no real orders)" if mode == "PAPER" else ""),
         f"Regime: {res.regime}",
         f"Equity Rs{res.equity:,.0f} ({res.equity / start - 1:+.2%}) | Cash Rs{res.cash:,.0f}",
         f"Capital used: Rs{invested:,.0f} invested ({_share(invested, res.equity)}), "
         f"cash {_share(res.cash, res.equity)}"]
    if res.fills:
        L += ["Fills today:"] + [f"- {x}" for x in res.fills]
    if res.exits:
        L += ["Exits today:"] + [f"- {x}" for x in res.exits]
    trades = ledger.trades()
    closed_today = [t for t in trades if t["exit_date"] == res.d.isoformat()]
    if closed_today:
        L.append("Sold today (after costs):")
        L += [f"- {t['symbol']}: {_trade_line(t)}" for t in closed_today]
    pos = ledger.positions()
    L.append(f"Open positions: {len(pos)}")
    for p in pos:
        value = p["qty"] * p["last_close"]
        move = p["last_close"] / p["entry"] - 1
        L.append(f"- {p['symbol']} {p['qty']} @ {p['entry']:.2f}, close {p['last_close']:.2f} ({move:+.2%}, "
                 f"Rs{(p['last_close'] - p['entry']) * p['qty']:+,.0f}), value Rs{value:,.0f} = "
                 f"{_share(value, res.equity)} of equity, stop {p['stop']:.2f}")
    if trades:
        L += _closed_stats(trades, start)
    if res.new_orders:
        L.append("PLANNED for next session (not placed yet):")
        for o in res.new_orders:
            L.append(f"- BUY LIMIT {o['qty']} {o['symbol']} [{o.get('strategy', '')}] @ {o['entry']:.2f}, stop {o['stop']:.2f}, "
                     f"target {o['target']:.2f}, score {o['score']}")
    else:
        L.append("No new orders planned (NO TRADE is a valid outcome).")
    for w in getattr(res, "event_warnings", [])[:5]:
        L.append(f"Event warning: {w}")
    if res.risk_rejections:
        L.append(f"Blocked by risk engine: {len(res.risk_rejections)}")
    if res.data_issues:
        L.append(f"Data issues: {', '.join(list(res.data_issues)[:5])}")
    L += [f"Note: {n}" for n in res.notes[:3]]
    return "\n".join(L)


def _intraday_trade_line(t) -> str:
    return (f"{t['qty']} bought {t['entry']:.2f} on {t['entry_date']}, sold {t['exit']:.2f} [{t['reason']}], "
            f"net Rs{t['net']:+,.0f} ({_trade_pct(t):+.2%}) same session/day")


def _format_intraday_pick(p: dict) -> list[str]:
    sym = p.get("symbol", "?")
    lines = [
        f"- {sym} ({p.get('sector', '')}): BUY {p.get('qty', 0)} @ {p.get('entry', 0):.2f}, "
        f"stop {p.get('stop', 0):.2f}, target {p.get('target', 0):.2f}, score {p.get('score', 0):.1f}, "
        f"R/R {p.get('gross_rr', 0):.2f}",
        f"  Capital used: Rs{p.get('capital_used', 0):,.0f} ({p.get('capital_pct_of_equity', 0):.1f}% of equity at entry)",
    ]
    for e in p.get("evidence") or []:
        lines.append(f"  Why: {e}")
    if p.get("invalidation"):
        lines.append(f"  Invalid if: {p['invalidation']}")
    if p.get("summary"):
        lines.append(f"  {p['summary']}")
    return lines


def format_intraday_session(res, ledger, mode: str = "PAPER") -> str:
    """One intraday session — P&L, capital used, picks with reasons (swing-style detail)."""
    from app.intraday.telegram_ctx import picks_on_day

    if res.skipped:
        return f"{mode} intraday {res.d}: no session / no 5m data.\n" + "\n".join(res.notes)
    start = ledger.getf("start_capital")
    invested = res.equity - res.cash
    sess_start = getattr(res, "session_start_equity", None) or ledger.getf("session_start_equity")
    sess_pnl = getattr(res, "session_pnl", res.equity - sess_start)
    sess_pct = getattr(res, "session_pnl_pct", sess_pnl / sess_start if sess_start else 0.0)
    L = [
        f"{mode} intraday report {res.d}" + (" (no real orders)" if mode == "PAPER" else ""),
        "Strategy: ORB long-only, 5m bars, MIS-style paper",
        f"Session P&L: Rs{sess_pnl:+,.0f} ({sess_pct:+.2%} on Rs{sess_start:,.0f} at open)",
        f"Equity Rs{res.equity:,.0f} ({res.equity / start - 1:+.2%} since start Rs{start:,.0f}) | Cash Rs{res.cash:,.0f}",
        f"Capital in market now: Rs{invested:,.0f} ({_share(invested, res.equity)}), "
        f"cash {_share(res.cash, res.equity)}",
    ]
    if res.benchmark:
        L.append(f"NIFTY close (5m): {res.benchmark:.2f}")

    picks = list(getattr(res, "new_entries", None) or []) or picks_on_day(ledger, res.d)
    if picks:
        L.append("Stocks picked (paper fills):")
        for p in picks:
            L += _format_intraday_pick(p)
    elif not res.fills:
        L.append("No stock picked this session (NO TRADE is valid).")

    if res.exits:
        L += ["Exits this session:"] + [f"- {x}" for x in res.exits]
    closed_today = [t for t in ledger.trades() if t["exit_date"] == res.d.isoformat()]
    if closed_today:
        L.append("Realized today (after costs):")
        for t in closed_today:
            L.append(f"- {t['symbol']}: {_intraday_trade_line(t)}")
    pos = ledger.positions()
    L.append(f"Open positions: {len(pos)} (expect 0 after EOD flat)")
    for p in pos:
        value = p["qty"] * p["last_close"]
        move = p["last_close"] / p["entry"] - 1
        u = (p["last_close"] - p["entry"]) * p["qty"]
        L.append(
            f"- {p['symbol']} {p['qty']} @ {p['entry']:.2f}, close {p['last_close']:.2f} ({move:+.2%}, "
            f"unrealized Rs{u:+,.0f}), value Rs{value:,.0f} = {_share(value, res.equity)} of equity, stop {p['stop']:.2f}"
        )
    trades = ledger.trades()
    if trades:
        L += _closed_stats(trades, start)
    if res.rejections and not picks:
        top = sorted(res.rejections.items(), key=lambda x: x[0])[:8]
        L.append("Sample rejections (no qualifying ORB at last bar):")
        for sym, why in top:
            L.append(f"- {sym}: {why[:120]}")
    L += [f"Note: {n}" for n in res.notes[:3]]
    return "\n".join(L)


def format_intraday_backtest(ledger, start: date, end: date, sessions: int, skipped: int,
                             last_res=None, mode: str = "PAPER") -> str:
    """Multi-day backtest: summary, every trade P&L %, every pick with reasons."""
    from app.intraday.telegram_ctx import picks_between
    from app.paper.report import summary as ledger_summary

    start_cap = ledger.getf("start_capital")
    eq_rows = ledger.con.execute("SELECT equity FROM equity ORDER BY d").fetchall()
    final = eq_rows[-1]["equity"] if eq_rows else start_cap
    trades = ledger.trades()
    L = [
        f"{mode} intraday BACKTEST {start} to {end} (no real orders)",
        f"Sessions run: {sessions}, skipped (no data): {skipped}",
        ledger_summary(ledger),
        f"Final equity Rs{final:,.0f} ({final / start_cap - 1:+.2%} on Rs{start_cap:,.0f} start)",
    ]
    if trades:
        L.append(f"All closed trades ({len(trades)}):")
        for t in trades:
            L.append(f"- {t['exit_date']} {t['symbol']}: {_intraday_trade_line(t)}")
    picks = picks_between(ledger, start, end)
    if picks:
        L.append(f"All picks with reasons ({len(picks)}):")
        for d, p in picks:
            L.append(f"[{d}]")
            L += _format_intraday_pick(p)
    if last_res is not None and not last_res.skipped:
        L += ["", "--- Last session detail ---", format_intraday_session(last_res, ledger, mode)]
    return "\n".join(L)
