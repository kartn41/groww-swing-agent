"""Markdown session / backtest reports for the intraday paper engine."""
from __future__ import annotations
from collections import Counter
from datetime import date
from typing import Optional

from app.intraday.config import IntradayConfig, load_intraday_config
from app.intraday.engine import SessionResult
from app.intraday.stats import accounting_check, benchmark_return, max_drawdown, split_trades, stats_lines, verdict
from app.paper.ledger import Ledger


def render(res: SessionResult, ledger: Ledger) -> str:
    start = ledger.getf("start_capital")
    L = [f"# PAPER intraday report: {res.d}", "", "_Intraday paper (MIS-style simulation): no real orders._", ""]
    if res.skipped:
        return "\n".join(L + res.notes) + "\n"
    L += [
        "## Account",
        f"- Session P&L: Rs{res.session_pnl:+,.0f} ({res.session_pnl_pct:+.2%})",
        f"- Equity: Rs{res.equity:,.0f} ({res.equity / start - 1:+.2%} since start Rs{start:,.0f})",
        f"- Cash: Rs{res.cash:,.0f}",
        f"- NIFTY close: {res.benchmark:.2f}" if res.benchmark else "- NIFTY: n/a",
        "",
        "## Picks",
    ]
    if res.new_entries:
        for p in res.new_entries:
            L.append(f"### {p['symbol']} ({p['sector']}): BUY {p['qty']} @ {p['entry']:.2f}, stop {p['stop']:.2f}, "
                     f"target {p['target']:.2f}, Rs{p['capital_used']:,.0f} ({p['capital_pct_of_equity']:.1f}% of equity)")
            L += [f"- {e}" for e in p["evidence"]]
    else:
        L.append("- none (NO TRADE is a valid outcome)")
    L += ["", "## Exits"] + ([f"- {x}" for x in res.exits] or ["- none"])
    if res.rejections:
        counts = Counter(r.split(" ", 1)[1].split(" (")[0][:60] for r in res.rejections.values())
        L += ["", "## Breakouts rejected by filters (counts)"] + [f"- {n} x {r}" for r, n in counts.most_common(8)]
    if res.data_issues:
        L += ["", f"## Skipped for incomplete data: {', '.join(sorted(res.data_issues))}"]
    if res.notes:
        L += ["", "## Notes"] + [f"- {n}" for n in res.notes]
    L += ["", "## Limitations", "- Cost rates are placeholders until checked against Groww's intraday charges.",
          "- Fills simulated on 5m bars; real fills can be worse."]
    return "\n".join(L) + "\n"


def _settings_lines(cfg: IntradayConfig) -> list[str]:
    return [
        f"- Entries {cfg.or_end:%H:%M}-{cfg.entry_end:%H:%M}, flat by {cfg.flat_by:%H:%M}; "
        f"max {cfg.max_trades_per_day} trade(s)/day, stop after a loss: {cfg.stop_after_loss}",
        f"- Risk {cfg.risk_per_trade_pct}% of equity per trade, position cap {cfg.max_position_pct}%, "
        f"daily loss limit {cfg.daily_loss_limit_pct}%",
        f"- Filters: OR width {cfg.min_or_pct}-{cfg.max_or_pct}%, volume {cfg.volume_mult}x, RS {cfg.min_rs_pct}%, "
        f"VWAP {cfg.require_vwap}, NIFTY filter {cfg.require_index_green}, min score {cfg.min_score}",
        f"- Exits: target {cfg.target_r}R, breakeven at {cfg.breakeven_at_r}R, stop {cfg.min_risk_pct}-{cfg.max_risk_pct}%",
    ]


def backtest_report(ledger: Ledger, start: date, end: date, sessions: int, skipped: int,
                    holdout: Optional[date] = None) -> str:
    cap = ledger.getf("start_capital")
    trades = ledger.trades()
    bench = benchmark_return(ledger)
    _, acct = accounting_check(ledger)
    L = [f"# PAPER intraday backtest {start} to {end}", "",
         f"_Simulation only. Sessions processed: {sessions}, skipped: {skipped}._", "",
         "## Result", f"- Equity Rs{ledger.equity_now():,.0f} ({ledger.equity_now() / cap - 1:+.2%} on Rs{cap:,.0f}), "
         f"max drawdown {max_drawdown(ledger):.2%}",
         f"- NIFTY same days: {bench:+.2%}" if bench is not None else "- NIFTY: n/a",
         f"- Accounting check: {acct}", ""]
    dev, test = split_trades(trades, holdout)
    groups = ([(dev, f"Development (before {holdout})"), (test, f"Holdout ({holdout} onward)")] if holdout
              else [(trades, "All trades")])
    for tr, label in groups:
        L += ["```"] + stats_lines(tr, cap, label) + ["```"]
    flags = verdict(ledger, holdout)
    L += ["", "## Red flags"] + ([f"- {f}" for f in flags] or ["- none found (still paper; confirm on more data)"])
    L += ["", "## Settings used"] + _settings_lines(load_intraday_config())
    if trades:
        L += ["", "## Trades", "| Date | Symbol | Entry | Exit | Reason | Net | R |", "|---|---|---|---|---|---|---|"]
        for t in trades[-100:]:
            r = t["net"] / t["risk"] if t["risk"] else 0.0
            L.append(f"| {t['exit_date']} | {t['symbol']} | {t['entry']:.2f} | {t['exit']:.2f} | "
                     f"{t['reason'].replace('INTRADAY_', '')} | Rs{t['net']:+,.0f} | {r:+.2f} |")
    return "\n".join(L) + "\n"
