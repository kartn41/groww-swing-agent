"""Markdown session / backtest reports for the intraday paper engine."""
from __future__ import annotations
from collections import Counter
from datetime import date

from app.intraday.engine import SessionResult
from app.paper.ledger import Ledger
from app.paper.report import summary as ledger_summary


def render(res: SessionResult, ledger: Ledger) -> str:
    start = ledger.getf("start_capital")
    L = [f"# PAPER intraday report: {res.d}", "", "_Intraday paper (MIS-style simulation): no real orders._", ""]
    if res.skipped:
        return "\n".join(L + res.notes) + "\n"
    ret = res.equity / start - 1
    L += [
        "## Account",
        f"- Equity: Rs{res.equity:,.0f} ({ret:+.2%} since start Rs{start:,.0f})",
        f"- Cash: Rs{res.cash:,.0f}",
        f"- NIFTY close (5m): {res.benchmark:.2f}" if res.benchmark else "- NIFTY: n/a",
        "",
        "## Strategy",
        "Opening-range breakout (long-only), 5-minute bars; flat by ~15:15 IST.",
        "",
        "## Fills",
    ]
    L += [f"- {x}" for x in res.fills] or ["- none"]
    L += ["", "## Exits"]
    L += [f"- {x}" for x in res.exits] or ["- none"]
    pos = ledger.positions()
    L += ["", "## Open positions (should be flat EOD)"]
    if pos:
        for p in pos:
            u = (p["last_close"] - p["entry"]) * p["qty"]
            L.append(f"- {p['symbol']} {p['qty']} @ {p['entry']:.2f}, close {p['last_close']:.2f}, "
                     f"unrealized Rs{u:,.0f}, stop {p['stop']:.2f}")
    else:
        L.append("- none")
    closed = [t for t in ledger.trades() if t["exit_date"] == res.d.isoformat()]
    if closed:
        L += ["", "## Closed today"]
        for t in closed:
            L.append(f"- {t['symbol']}: {t['qty']} @ {t['entry']:.2f} -> {t['exit']:.2f} [{t['reason']}], "
                     f"net Rs{t['net']:+,.0f}")
    if res.rejections:
        counts = Counter(r.split(" (")[0].strip()[:60] for r in res.rejections.values())
        L += ["", "## Rejection reasons (last bar scan, sample counts)"] + [f"- {n} x {r}" for r, n in counts.most_common(8)]
    if res.notes:
        L += ["", "## Notes"] + [f"- {n}" for n in res.notes]
    L += ["", "## Limitations", "- Costs use delivery placeholders; MIS fees differ.", "- 5m bar fills are optimistic."]
    return "\n".join(L) + "\n"


def backtest_report(ledger: Ledger, start: date, end: date, sessions: int, skipped: int) -> str:
    body = ledger_summary(ledger)
    header = (f"# PAPER intraday backtest {start} to {end}\n\n"
              f"_Simulation only; GROWW_MODE=PAPER. Sessions processed: {sessions}, skipped: {skipped}._\n\n"
              f"## Summary\n{body}\n")
    tr = ledger.trades()
    if tr:
        header += "\n## Trades\n| Symbol | Entry | Exit | Reason | Net |\n|---|---|---|---|---|\n"
        for t in tr[-50:]:
            header += (f"| {t['symbol']} | {t['entry_date']} {t['entry']:.2f} | {t['exit_date']} {t['exit']:.2f} | "
                       f"{t['reason']} | Rs{t['net']:+,.0f} |\n")
        if len(tr) > 50:
            header += f"\n_(Showing last 50 of {len(tr)} trades.)_\n"
    return header
