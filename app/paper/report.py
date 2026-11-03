"""Markdown daily report. Every number comes from the ledger; nothing is invented."""
from __future__ import annotations
from collections import Counter
from app.paper.engine import DayResult
from app.paper.ledger import Ledger


def render(res: DayResult, ledger: Ledger) -> str:
    start = ledger.getf("start_capital")
    L = [f"# PAPER report: {res.d}", "", "_Paper trading: no real orders were placed._", ""]
    if res.skipped:
        return "\n".join(L + res.notes) + "\n"
    ret = res.equity / start - 1
    bstart = ledger.get("benchmark_start")
    bench = (res.benchmark / float(bstart) - 1) if bstart and res.benchmark else None
    L += ["## Account",
          f"- Equity: Rs{res.equity:,.0f} ({ret:+.2%} since start of Rs{start:,.0f})",
          f"- Cash: Rs{res.cash:,.0f}",
          f"- NIFTY over same period: {bench:+.2%}" if bench is not None else "- NIFTY benchmark: n/a", "",
          "## Market regime", f"**{res.regime}**: {res.regime_detail}", ""]
    L += ["## Today's fills"] + ([f"- {x}" for x in res.fills] or ["- none"]) + [""]
    L += ["## Today's exits"] + ([f"- {x}" for x in res.exits] or ["- none"]) + [""]
    pos = ledger.positions()
    L += ["## Open positions"]
    if pos:
        L += ["| Symbol | Qty | Entry | Close | Stop | Target | Unrealized |", "|---|---|---|---|---|---|---|"]
        for p in pos:
            u = (p["last_close"] - p["entry"]) * p["qty"]
            flag = " (exit next open)" if p["exit_pending"] else ""
            L.append(f"| {p['symbol']}{flag} | {p['qty']} | {p['entry']:.2f} | {p['last_close']:.2f} | "
                     f"{p['stop']:.2f} | {p['target']:.2f} | Rs{u:,.0f} |")
    else:
        L.append("- none")
    L += ["", "## Orders planned for next session"]
    if res.new_orders:
        for o in res.new_orders:
            L += [f"### {o['symbol']} ({o.get('strategy', '')}): score {o['score']}",
                  f"BUY LIMIT {o['qty']} @ {o['entry']:.2f}, stop {o['stop']:.2f}, target {o['target']:.2f}, "
                  f"net reward/risk {o['net_rr']}", f"Cancel if: {o['invalidation']}"]
            L += [f"- {e}" for e in o["evidence"]]
    else:
        L.append("- none (NO TRADE is a valid outcome)")
    if res.risk_rejections:
        L += ["", "## Setups blocked by the risk engine"] + [f"- {s}: {r}" for s, r in res.risk_rejections.items()]
    if res.rejections:
        counts = Counter(part.split(" (")[0].strip()[:70] for r in res.rejections.values() for part in r.split(" | "))
        L += ["", "## Why other stocks were rejected (counts)"] + [f"- {n} x {r}" for r, n in counts.most_common()]
    if res.data_issues:
        L += ["", "## Data issues (symbols excluded from new entries)"] + [f"- {s}: {i}" for s, i in res.data_issues.items()]
    if res.event_warnings:
        L += ["", "## Upcoming events on held stocks (not auto-sold; review manually)"] + [f"- {w}" for w in res.event_warnings]
    if res.notes:
        L += ["", "## Notes"] + [f"- {n}" for n in res.notes]
    L += ["", "## Known limitations",
          "- Cost rates in config are placeholders until replaced with Groww's actual charges.",
          "- Fills simulated on daily candles; real fills can be worse."]
    return "\n".join(L) + "\n"


def summary(ledger: Ledger) -> str:
    tr = ledger.trades()
    start = ledger.getf("start_capital")
    eq = ledger.con.execute("SELECT d, equity, benchmark FROM equity ORDER BY d").fetchall()
    if not eq:
        return "No processed days."
    peak, mdd = 0.0, 0.0
    for r in eq:
        peak = max(peak, r["equity"])
        mdd = max(mdd, 1 - r["equity"] / peak)
    wins = [t for t in tr if t["net"] > 0]
    net = sum(t["net"] for t in tr)
    b0, b1 = eq[0]["benchmark"], eq[-1]["benchmark"]
    lines = [f"Days processed: {len(eq)} ({eq[0]['d']} to {eq[-1]['d']})",
             f"Closed trades: {len(tr)}" + (f", win rate {len(wins) / len(tr):.0%}" if tr else ""),
             f"Realized net P&L: Rs{net:,.0f}",
             f"Equity: Rs{eq[-1]['equity']:,.0f} ({eq[-1]['equity'] / start - 1:+.2%}); max drawdown {mdd:.2%}",
             f"NIFTY same period: {b1 / b0 - 1:+.2%}" if b0 and b1 else "NIFTY: n/a"]
    if len(tr) < 30:
        lines.append(f"Note: {len(tr)} trades is too few to judge the strategy statistically.")
    return "\n".join(lines)
