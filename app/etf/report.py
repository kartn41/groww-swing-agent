"""GoldBEES / SilverBEES daily report (Telegram text + markdown file) and market-hours alerts.
Every number comes from the ledger or the analysis; planned orders are labelled as plans, never as fills."""
from __future__ import annotations
from datetime import date

from app.etf.engine import EodResult
from app.etf.ledger import EtfLedger


def _signal(sym: str, res: EodResult, ledger: EtfLedger) -> str:
    if sym in res.exits_flagged:
        return f"SELL next session ({res.exits_flagged[sym]})"
    planned = [o for o in res.new_orders if o["symbol"] == sym]
    if planned:
        o = planned[0]
        return f"BUY tranche {o['tranche']} ({o['kind']}) next session"
    return "HOLD" if ledger.position(sym) else "NO TRADE (cash)"


def _next_action(sym: str, res: EodResult, ledger: EtfLedger) -> str:
    p = ledger.position(sym)
    planned = [o for o in res.new_orders if o["symbol"] == sym]
    if sym in res.exits_flagged:
        return "sell the whole position in the 09:45-15:00 window"
    if planned:
        o = planned[0]
        return (f"buy up to {o['qty']} @ <= {o['limit']:.2f} between 09:45 and 15:00 if 1h/15m candles confirm; "
                f"stop {o['stop']:.2f}, target {o['target']:.2f}")
    if p:
        return (f"hold; sell if price <= {p['stop']:.2f} (stop), on a sudden fall, or at the target {p['target']:.2f}")
    return "wait in cash: " + res.rejections.get(sym, "no qualifying setup")


def format_eod(res: EodResult, ledger: EtfLedger, mode: str = "PAPER", fallback_note: str = "") -> str:
    start = ledger.getf("start_capital")
    realized = ledger.realized()
    realized_today = ledger.realized(res.d) - ledger.realized(date.fromordinal(res.d.toordinal() + 1))
    L = [f"{mode} GoldBEES/SilverBEES report {res.d}" + (" (no real orders)" if mode == "PAPER" else ""),
         f"Equity Rs{res.equity:,.0f} ({res.equity / start - 1:+.2%} since start) | "
         f"available cash Rs{res.cash:,.0f}" + (f" + unsettled Rs{res.unsettled:,.0f}" if res.unsettled else ""),
         f"Realized P&L: today Rs{realized_today:+,.0f}, total Rs{realized:+,.0f} (after charges, before income tax)"]
    if fallback_note:
        L.append(fallback_note)
    if res.risk_block:
        L.append(f"NEW BUYS BLOCKED: {res.risk_block}")
    for sym in ("GOLDBEES", "SILVERBEES"):
        s = res.snapshots.get(sym)
        L.append("")
        if s is None:
            L.append(f"{sym}: {res.data_issues.get(sym, 'no data')}")
            continue
        L += [f"{sym} {s.close:.2f} ({s.close / s.prev_close - 1:+.2%}) | regime {s.regime} | {s.pullback}",
              f"  {s.summary()}",
              f"  Signal: {_signal(sym, res, ledger)}"]
        p = ledger.position(sym)
        if p:
            u = (s.close - p["avg"]) * p["qty"]
            L.append(f"  Position: {p['qty']} units, avg {p['avg']:.2f}, {p['tranches']}/3 tranches, "
                     f"unrealized Rs{u:+,.0f} ({s.close / p['avg'] - 1:+.2%}), stop {p['stop']:.2f}, "
                     f"target {p['target']:.2f}" + (" (extended)" if p["extended"] else ""))
            for t in ledger.tranches(sym):
                L.append(f"    T{t['tranche_no']} {t['kind']}: {t['qty']} @ {t['price']:.2f} on {t['d']}")
            for n in res.position_notes.get(sym, []):
                L.append(f"  - {n}")
        L.append(f"  Next: {_next_action(sym, res, ledger)}")
    for o in res.new_orders:
        L += ["", f"PLANNED {o['symbol']} T{o['tranche']} {o['kind']}: BUY LIMIT {o['qty']} @ {o['limit']:.2f}, "
                  f"stop {o['stop']:.2f}, target {o['target']:.2f}, score {o['score']}, net R:R {o['net_rr']}, "
                  f"max loss Rs{o['risk']:,.0f}", "Why:"] + [f"- {r}" for r in o["reasons"][:6]]
    closed = [t for t in ledger.trades() if t["exit_date"] == res.d.isoformat()]
    if closed:
        L += ["", "Sold today:"] + [f"- {t['symbol']} {t['qty']} @ {t['exit']:.2f} (avg {t['entry']:.2f}) [{t['reason']}] "
                                    f"net Rs{t['net']:+,.0f}" for t in closed]
    L += [f"Note: {n}" for n in res.notes[:3]]
    return "\n".join(L)


def render_md(res: EodResult, ledger: EtfLedger, mode: str = "PAPER", fallback_note: str = "") -> str:
    body = format_eod(res, ledger, mode, fallback_note)
    rej = "\n".join(f"- {k}: {v}" for k, v in res.rejections.items()) or "- none"
    return f"# {mode} ETF report {res.d}\n\n```\n{body}\n```\n\n## Why no other trade\n{rej}\n"


def format_cycle(res, mode: str = "PAPER") -> str:
    """Market-hours message; empty string when nothing happened (no Telegram noise)."""
    if not (res.fills or res.exits or res.alerts):
        return ""
    L = [f"{mode} ETF monitor {res.at:%Y-%m-%d %H:%M}"]
    L += [f"- {x}" for x in res.alerts]
    L += [f"- {x}" for x in res.exits]
    L += [f"- {x}" for x in res.fills]
    for sym, q in sorted(res.quotes.items()):
        L.append(f"{sym} {q.ltp:.2f}" + (f" (bid {q.bid:.2f} / ask {q.ask:.2f})" if q.bid and q.ask else ""))
    return "\n".join(L)
