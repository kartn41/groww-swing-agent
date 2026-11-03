"""Local dashboard (standard library only, ~20 MB RAM).

Security: binds 127.0.0.1 only (reach it through an SSH tunnel), HTTP Basic auth with
DASHBOARD_PASSWORD (refuses to start without it), and the single state-changing action (the
new-entries switch) requires a per-process token. Nothing here can place or modify orders.
"""
from __future__ import annotations
import base64
import hmac
import html
import os
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from app.config import Settings
from app.logging_setup import redact, secret_values

e = html.escape


@dataclass
class DashConfig:
    paper_db: str
    market_db: str
    reports_dir: str
    log_file: str
    settings: Settings
    password: str
    host: str = "127.0.0.1"
    port: int = 8501


CSS = """
:root{--bg:#eef2f6;--surface:#fff;--ink:#1b2735;--muted:#5a6878;--line:#d5dde6;--gain:#1d7a57;--loss:#b03636;
--paper:#2b5aa6;--live:#9a5800;--halt:#7b2d8e;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#10161d;--surface:#18212b;--ink:#e3e9f0;--muted:#97a6b6;--line:#2a3643;
--gain:#4fbf8e;--loss:#ef7a7a;--paper:#6f9be3;--live:#e0a24a;--halt:#c98ad9;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 "Source Sans 3","Segoe UI",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.band{padding:22px clamp(16px,4vw,48px);color:#fff;display:flex;flex-wrap:wrap;gap:12px 32px;align-items:baseline}
.band.paper{background:var(--paper)}.band.live{background:var(--live)}.band.halt{background:var(--halt)}
.band h1{margin:0;font-size:clamp(28px,4vw,40px);font-weight:700;letter-spacing:-.01em}
.band p{margin:0;opacity:.92}.band form{margin-left:auto}
button{font:inherit;font-weight:600;padding:10px 18px;border-radius:6px;border:2px solid #fff;background:transparent;
color:#fff;cursor:pointer}button:hover{background:rgba(255,255,255,.15)}button:focus-visible,a:focus-visible{outline:3px solid #ffd54a;outline-offset:2px}
main{max-width:1100px;margin:0 auto;padding:24px clamp(16px,4vw,48px) 64px}
nav{display:flex;gap:20px;margin-bottom:8px}nav a{color:var(--muted)}
h2{font-size:21px;margin:36px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.figures{display:flex;flex-wrap:wrap;gap:8px 40px}.figures div{min-width:150px}.figures b{display:block;font-size:24px}
.figures span{color:var(--muted);font-size:14px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--surface)}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:15px;vertical-align:top}
th{color:var(--muted);font-weight:600}td.n{text-align:right}.gain{color:var(--gain)}.loss{color:var(--loss)}
.muted{color:var(--muted)}pre{white-space:pre-wrap;background:var(--surface);padding:12px;border:1px solid var(--line);font-size:13px}
svg{width:100%;height:auto;background:var(--surface);border:1px solid var(--line)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _q(db: str, sql: str, args=()):
    if not os.path.exists(db):
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _money(v, sign=False) -> str:
    cls = "" if not sign else ("gain" if v > 0 else "loss" if v < 0 else "")
    return f'<span class="{cls}">{"+" if sign and v > 0 else ""}Rs{v:,.0f}</span>'


def equity_svg(rows) -> str:
    if len(rows) < 2:
        return '<p class="muted">The chart appears after two processed trading days.</p>'
    eq = [r["equity"] for r in rows]
    bm = [r["benchmark"] or 0 for r in rows]
    a = [v / eq[0] for v in eq]
    b = [v / bm[0] if bm[0] else 1 for v in bm]
    lo, hi = min(a + b), max(a + b)
    span = (hi - lo) or 1
    W, H, P = 900, 240, 12

    def pts(series):
        n = len(series) - 1
        return " ".join(f"{P + i * (W - 2 * P) / n:.1f},{H - P - (v - lo) / span * (H - 2 * P):.1f}" for i, v in enumerate(series))
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Paper equity versus NIFTY, indexed to 1">'
            f'<polyline fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="5 4" points="{pts(b)}"/>'
            f'<polyline fill="none" stroke="var(--paper)" stroke-width="2.5" points="{pts(a)}"/></svg>'
            '<p class="muted">Solid: paper account. Dashed: NIFTY. Both start at the same point.</p>')


def page(cfg: DashConfig, token: str, kill_active: bool) -> str:
    s = cfg.settings
    meta = {r["key"]: r["value"] for r in _q(cfg.paper_db, "SELECT key, value FROM meta")}
    last = meta.get("last_processed")
    nifty_last = _q(cfg.market_db, "SELECT MAX(d) AS d FROM candles WHERE symbol='NSE-NIFTY'")
    nifty_last = nifty_last[0]["d"] if nifty_last else None
    mode = s.mode.value
    band = "halt" if kill_active else ("live" if mode == "LIVE" else "paper")
    state = "New entries stopped" if kill_active else "New entries allowed"
    action = "Allow new entries" if kill_active else "Stop new entries"
    H = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
         f"content='width=device-width,initial-scale=1'><title>{mode} agent</title><style>{CSS}</style></head><body>",
         f"<header class='band {band}'><h1>{mode} mode. {state}.</h1>"
         f"<p>Last processed day: {e(last or 'none yet')}. Latest NIFTY candle: {e(nifty_last or 'none')}.</p>"
         f"<form method='post' action='/kill'><input type='hidden' name='token' value='{token}'>"
         f"<button type='submit'>{action}</button></form></header><main>"
         "<nav><a href='/'>Overview</a><a href='/backtests'>Backtests</a><a href='/logs'>Logs</a></nav>"]
    if not meta:
        H.append("<h2>No paper account yet</h2><p>Run <code>python -m app.run daily</code> after 15:45 IST, or wait "
                 "for the scheduled run. This page fills in after the first processed day.</p></main></body></html>")
        return "".join(H)
    start = float(meta.get("start_capital", 0) or 0)
    eqrows = _q(cfg.paper_db, "SELECT d, equity, benchmark FROM equity ORDER BY d")
    equity = eqrows[-1]["equity"] if eqrows else start
    cash = float(meta.get("cash", 0))

    def ret_since(n):
        if len(eqrows) <= n:
            return None
        return eqrows[-1]["equity"] / eqrows[-1 - n]["equity"] - 1
    figs = [("Equity", _money(equity)), ("Since start", f"{equity / start - 1:+.2%}" if start else "n/a"),
            ("Cash", _money(cash)), ("Last day", f"{ret_since(1):+.2%}" if ret_since(1) is not None else "n/a"),
            ("Last 5 days", f"{ret_since(5):+.2%}" if ret_since(5) is not None else "n/a"),
            ("Last 21 days", f"{ret_since(21):+.2%}" if ret_since(21) is not None else "n/a")]
    peak = max((r["equity"] for r in eqrows), default=equity)
    figs.append(("Drawdown from peak", f"{equity / peak - 1:+.2%}" if peak else "n/a"))
    H.append("<h2>Account</h2><div class='figures'>" + "".join(f"<div><b>{v}</b><span>{k}</span></div>" for k, v in figs)
             + "</div>" + equity_svg(eqrows))

    pos = _q(cfg.paper_db, "SELECT * FROM positions ORDER BY symbol")
    H.append("<h2>Open positions</h2>")
    if pos:
        H.append("<div class='scroll'><table><tr><th>Stock</th><th>Strategy</th><th>Qty</th><th>Entry</th><th>Close</th>"
                 "<th>Stop</th><th>Target</th><th>Unrealized</th></tr>")
        for p in pos:
            u = (p["last_close"] - p["entry"]) * p["qty"]
            flag = " (exit at next open)" if p["exit_pending"] else ""
            H.append(f"<tr><td>{e(p['symbol'])}{flag}</td><td>{e(p['strategy'] or '')}</td><td class=n>{p['qty']}</td>"
                     f"<td class=n>{p['entry']:.2f}</td><td class=n>{p['last_close']:.2f}</td><td class=n>{p['stop']:.2f}</td>"
                     f"<td class=n>{p['target']:.2f}</td><td class=n>{_money(u, True)}</td></tr>")
        H.append("</table></div>")
    else:
        H.append("<p class='muted'>No open positions.</p>")

    planned = _q(cfg.paper_db, "SELECT * FROM orders WHERE status='PENDING' ORDER BY score DESC")
    H.append("<h2>Planned for the next session</h2>")
    if planned:
        H.append("<div class='scroll'><table><tr><th>Stock</th><th>Strategy</th><th>Qty</th><th>Buy limit</th><th>Stop</th>"
                 "<th>Target</th><th>Score</th></tr>")
        H += [f"<tr><td>{e(o['symbol'])}</td><td>{e(o['strategy'] or '')}</td><td class=n>{o['qty']}</td>"
              f"<td class=n>{o['limit_price']:.2f}</td><td class=n>{o['stop']:.2f}</td><td class=n>{o['target']:.2f}</td>"
              f"<td class=n>{o['score']:.0f}</td></tr>" for o in planned]
        H.append("</table></div><p class='muted'>Paper orders: simulated, never sent to Groww.</p>")
    else:
        H.append("<p class='muted'>Nothing planned. Taking no trade is a valid outcome.</p>")

    trades = _q(cfg.paper_db, "SELECT * FROM trades ORDER BY id DESC LIMIT 20")
    H.append("<h2>Recent closed trades</h2>")
    if trades:
        H.append("<div class='scroll'><table><tr><th>Stock</th><th>Strategy</th><th>Entry</th><th>Exit</th><th>Reason</th>"
                 "<th>Net P&amp;L</th></tr>")
        H += [f"<tr><td>{e(t['symbol'])}</td><td>{e(t['strategy'] or '')}</td><td>{e(t['entry_date'])} @ {t['entry']:.2f}</td>"
              f"<td>{e(t['exit_date'])} @ {t['exit']:.2f}</td><td>{e(t['reason'])}</td><td class=n>{_money(t['net'], True)}</td></tr>"
              for t in trades]
        H.append("</table></div>")
    else:
        H.append("<p class='muted'>No closed trades yet.</p>")

    if last:
        rej = _q(cfg.paper_db, "SELECT kind, detail FROM events WHERE d=? AND kind IN ('REJECTED','RISK_REJECTED')", (last,))
        c = Counter()
        for r in rej:
            for part in (r["detail"] or "").split(" | "):
                c[part.split(" (")[0].strip()[:90]] += 1
        H.append(f"<h2>Why stocks were not bought on {e(last)}</h2>")
        H.append("<div class='scroll'><table><tr><th>Reason</th><th>Count</th></tr>" +
                 "".join(f"<tr><td>{e(k)}</td><td class=n>{v}</td></tr>" for k, v in c.most_common(12)) + "</table></div>"
                 if c else "<p class='muted'>No rejections recorded.</p>")

    lim = s.risk
    H.append("<h2>Risk limits in force</h2><div class='scroll'><table>" + "".join(
        f"<tr><td>{e(k)}</td><td class=n>{v}</td></tr>" for k, v in (
            ("Risk per trade", f"{lim.risk_per_trade_pct}% of equity"), ("Total open risk", f"{lim.max_open_risk_pct}%"),
            ("Max positions", lim.max_positions), ("Max per stock", f"{lim.max_position_alloc_pct}%"),
            ("Max per sector", f"{lim.max_sector_alloc_pct}%"), ("Minimum reward/risk after costs", lim.min_reward_risk),
            ("Daily loss limit", f"{lim.daily_loss_limit_pct}%"), ("Weekly drawdown limit", f"{lim.weekly_drawdown_limit_pct}%"),
            ("Max drawdown", f"{lim.max_drawdown_pct}%"))) + "</table></div>"
             "<p class='muted'>Limits are set in <code>app/config.py</code> and change only through a code edit.</p>")
    H.append("</main></body></html>")
    return "".join(H)


def backtests_page(cfg: DashConfig) -> str:
    files = sorted(Path(cfg.reports_dir).glob("backtest_*.md"), reverse=True)[:10] if os.path.isdir(cfg.reports_dir) else []
    body = "".join(f"<h2>{e(f.name)}</h2><pre>{e(f.read_text())}</pre>" for f in files) or \
        "<p class='muted'>No backtests yet. Run <code>python -m app.run backtest --start 2021-01-01 --holdout 2025-01-01</code>.</p>"
    return _shell("Backtests", body)


def logs_page(cfg: DashConfig) -> str:
    try:
        with open(cfg.log_file, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-200:]
    except OSError:
        tail = ["No log file yet."]
    return _shell("Logs", f"<pre>{e(redact(''.join(tail), secret_values()))}</pre>")


def _shell(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head><body><main>"
            f"<nav><a href='/'>Overview</a><a href='/backtests'>Backtests</a><a href='/logs'>Logs</a></nav>"
            f"<h2>{e(title)}</h2>{body}</main></body></html>")


def make_server(cfg: DashConfig) -> ThreadingHTTPServer:
    if not cfg.password or len(cfg.password) < 8:
        raise RuntimeError("Set DASHBOARD_PASSWORD (8+ characters) in .env before starting the dashboard")
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):          # keep request lines out of logs
            pass

        def _authed(self) -> bool:
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(pw.encode(), cfg.password.encode())

        def _send(self, code: int, body: str, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _challenge(self):
            self._send(401, "Authentication required", "text/plain", {"WWW-Authenticate": 'Basic realm="agent"'})

        def do_GET(self):
            if not self._authed():
                return self._challenge()
            kill = os.path.exists(cfg.settings.kill_switch_file)
            routes = {"/": lambda: page(cfg, token, kill), "/backtests": lambda: backtests_page(cfg),
                      "/logs": lambda: logs_page(cfg)}
            fn = routes.get(self.path.split("?")[0])
            if fn is None:
                return self._send(404, "Not found", "text/plain")
            self._send(200, fn())

        def do_POST(self):
            if not self._authed():
                return self._challenge()
            if self.path != "/kill":
                return self._send(404, "Not found", "text/plain")
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(n).decode())
            if not hmac.compare_digest((form.get("token") or [""])[0], token):
                return self._send(403, "Invalid form token. Reload the page and try again.", "text/plain")
            ks = cfg.settings.kill_switch_file
            if os.path.exists(ks):
                os.remove(ks)
            else:
                os.makedirs(os.path.dirname(ks), exist_ok=True)
                with open(ks, "w") as f:
                    f.write(f"set from dashboard {date.today()}\n")
            self._send(303, "", extra={"Location": "/"})

    return ThreadingHTTPServer((cfg.host, cfg.port), Handler)
