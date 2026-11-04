"""Backtest statistics and validation checks for the intraday ledger."""
from __future__ import annotations
from collections import Counter
from datetime import date
from typing import Optional

from app.paper.ledger import Ledger


def trade_stats(trades, start_capital: float) -> dict:
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    r_mults = [t["net"] / t["risk"] for t in trades if t["risk"]]
    gross_loss = -sum(losses)
    return {
        "trades": len(nets),
        "wins": len(wins),
        "win_rate": len(wins) / len(nets) if nets else 0.0,
        "net": sum(nets),
        "return_pct": sum(nets) / start_capital if start_capital else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": sum(wins) / gross_loss if gross_loss > 0 else (float("inf") if wins else 0.0),
        "avg_r": sum(r_mults) / len(r_mults) if r_mults else 0.0,
        "exit_reasons": Counter(t["reason"].replace("INTRADAY_", "") for t in trades),
    }


def max_drawdown(ledger: Ledger) -> float:
    peak, mdd = 0.0, 0.0
    for r in ledger.con.execute("SELECT equity FROM equity ORDER BY d"):
        peak = max(peak, r["equity"])
        if peak > 0:
            mdd = max(mdd, 1 - r["equity"] / peak)
    return mdd


def accounting_check(ledger: Ledger) -> tuple[bool, str]:
    """When flat, cash must equal start capital plus every closed trade's net P&L."""
    if ledger.positions():
        return True, "positions still open; check skipped"
    start = ledger.getf("start_capital")
    expected = start + sum(t["net"] for t in ledger.trades())
    actual = ledger.getf("cash")
    if abs(expected - actual) > 1.0:
        return False, f"FAILED: cash Rs{actual:,.0f} but start + trade P&L = Rs{expected:,.0f}"
    return True, f"OK: cash Rs{actual:,.0f} = start Rs{start:,.0f} + trade P&L Rs{expected - start:+,.0f}"


def benchmark_return(ledger: Ledger) -> Optional[float]:
    rows = ledger.con.execute("SELECT benchmark FROM equity WHERE benchmark IS NOT NULL ORDER BY d").fetchall()
    if len(rows) < 2 or not rows[0]["benchmark"]:
        return None
    return rows[-1]["benchmark"] / rows[0]["benchmark"] - 1


def split_trades(trades, holdout: Optional[date]):
    if holdout is None:
        return list(trades), []
    h = holdout.isoformat()
    return [t for t in trades if t["exit_date"] < h], [t for t in trades if t["exit_date"] >= h]


def verdict(ledger: Ledger, holdout: Optional[date] = None) -> list[str]:
    """Red flags that should stop anyone trusting the result."""
    start = ledger.getf("start_capital")
    trades = ledger.trades()
    flags = []
    ok, msg = accounting_check(ledger)
    if not ok:
        flags.append(f"Accounting check {msg}")
    if len(trades) < 30:
        flags.append(f"Only {len(trades)} trades: too few to judge (want 30+, ideally 100+)")
    dev, test = split_trades(trades, holdout)
    for label, tr in (("development", dev), ("holdout", test)):
        if not tr:
            continue
        s = trade_stats(tr, start)
        if s["profit_factor"] < 1.2:
            flags.append(f"{label}: profit factor {s['profit_factor']:.2f} < 1.2 (no reliable edge after costs)")
        if s["avg_r"] <= 0:
            flags.append(f"{label}: average trade {s['avg_r']:+.2f}R (loses money per trade)")
    bench = benchmark_return(ledger)
    if bench is not None and trades and trade_stats(trades, start)["return_pct"] < bench:
        flags.append(f"Return below NIFTY over the same days ({bench:+.2%})")
    return flags


def stats_lines(trades, start: float, label: str) -> list[str]:
    if not trades:
        return [f"{label}: no trades"]
    s = trade_stats(trades, start)
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    reasons = ", ".join(f"{k} {v}" for k, v in s["exit_reasons"].most_common())
    return [
        f"{label}: {s['trades']} trades, win rate {s['win_rate']:.0%}, net Rs{s['net']:+,.0f} "
        f"({s['return_pct']:+.2%} of capital)",
        f"  profit factor {pf}, average {s['avg_r']:+.2f}R per trade, "
        f"avg win Rs{s['avg_win']:,.0f}, avg loss Rs{s['avg_loss']:,.0f}",
        f"  exits: {reasons}",
    ]
