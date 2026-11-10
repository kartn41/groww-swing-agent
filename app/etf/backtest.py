"""GoldBEES / SilverBEES backtest on daily candles with the same decision code as paper trading.

Execution assumptions (all conservative): decisions on the close, limit orders for the next session only,
fills at the open or the limit, slippage + half-spread per side (profile.slippage_bps), Groww delivery charges
without STT, worst-case ordering inside a bar (stop before target), no same-day sale of a same-day purchase,
sale proceeds usable only from the next session. The 1-hour / 15-minute confirmation used in paper/live trading
cannot be replayed on daily bars, and intraday falls from the day's high are not visible either.
Survivorship bias does not apply (two fixed instruments chosen up front), but choosing gold and silver after
their 2024-2026 rally is itself a selection: treat bull-market results with suspicion."""
from __future__ import annotations
from dataclasses import replace
from datetime import date
from typing import Optional

import pandas as pd

from app.backtest import add_months, stats
from app.etf.config import EtfConfig
from app.etf.engine import end_of_day, simulate_session
from app.etf.ledger import EtfLedger

TAIL = 400


def run(frames: dict, symbols: list[str], start: date, end: date, cfg: EtfConfig,
        capital: Optional[float] = None) -> EtfLedger:
    led = EtfLedger(":memory:", capital or cfg.capital)
    days = sorted({d for s in symbols for d in frames[s].index if start <= d <= end})
    base = {}
    for d in days:
        simulate_session(d, led, {s: frames[s] for s in symbols}, cfg)
        sub = {s: frames[s].loc[:d].iloc[-TAIL:] for s in symbols}
        bench = []
        for s in symbols:
            if d in frames[s].index:
                c = float(frames[s].loc[d, "close"])
                base.setdefault(s, c)
                bench.append(c / base[s])
        end_of_day(d, led, sub, cfg, benchmark=sum(bench) / len(bench) if bench else None)
    led.commit()
    return led


def extra_stats(led: EtfLedger) -> dict:
    tr = pd.read_sql_query("SELECT * FROM trades", led.con)
    tz = pd.read_sql_query("SELECT * FROM tranches", led.con)
    out = {}
    if not tr.empty:
        out["by_symbol"] = {k: {"trades": len(g), "win_rate": float((g["net"] > 0).mean()), "net": float(g["net"].sum())}
                            for k, g in tr.groupby("symbol")}
        out["tranches_per_trade"] = float(tr["tranches"].mean())
    if not tz.empty:
        out["tranche_counts"] = tz.groupby("tranche_no").size().to_dict()
    return out


def evaluate(frames, symbols, start, end, cfg, capital=None) -> dict:
    led = run(frames, symbols, start, end, cfg, capital)
    s = stats(led)
    s.update(extra_stats(led))
    return s


def walk_forward(frames, symbols, start, end, cfg, months: int = 6) -> list[dict]:
    folds, s = [], start
    while s < end:
        e = min(add_months(s, months), end)
        st = evaluate(frames, symbols, s, e, cfg)
        st["fold"] = f"{s} to {e}"
        folds.append(st)
        s = date.fromordinal(e.toordinal() + 1)
    return folds


SENSITIVITY = {
    "baseline": lambda c: c,
    "slippage x3": lambda c: replace(c, profiles={k: replace(p, slippage_bps=p.slippage_bps * 3)
                                                  for k, p in c.profiles.items()}),
    "targets fixed 1.5/2.0/2.5% (no ATR scaling)": lambda c: replace(c, profiles={
        k: replace(p, target_atr=0.0) for k, p in c.profiles.items()}),
    "no sudden-drop exit": lambda c: replace(c, profiles={k: replace(p, sudden_drop_pct=99.0, sudden_drop_atr=99.0)
                                                          for k, p in c.profiles.items()}),
    "single tranche (100% at once)": lambda c: replace(c, tranche_weights=(1.0, 1e-9, 1e-9)),
    "min net R:R 1.5": lambda c: replace(c, min_net_rr=1.5),
}


def sensitivity(frames, symbols, start, end, cfg) -> dict:
    return {k: evaluate(frames, symbols, start, end, f(cfg)) for k, f in SENSITIVITY.items()}


def verdict(dev: dict, test: Optional[dict], folds: list[dict]) -> list[str]:
    flags = []
    n = dev.get("trades", 0)
    if n < 30:
        flags.append(f"Only {n} trades in development: too few to trust the statistics.")
    ci = dev.get("expectancy_r_ci95")
    if ci and ci[0] <= 0:
        flags.append(f"Expectancy 95% interval includes zero ({ci[0]:+.2f}R to {ci[1]:+.2f}R).")
    pf = dev.get("profit_factor")
    if pf is not None and pf < 1.2:
        flags.append(f"Profit factor {pf:.2f} < 1.2 in development.")
    if dev.get("total_return", 0) <= 0:
        flags.append("Lost money in development after costs.")
    if test is not None and test.get("total_return", 0) <= 0:
        flags.append("Lost money in the untouched holdout period.")
    if test is not None and test.get("trades", 0) and (test.get("profit_factor") or 0) < 1.0:
        flags.append("Holdout profit factor below 1.0.")
    if folds:
        pos = sum(1 for f in folds if f.get("total_return", 0) > 0)
        if pos / len(folds) < 0.6:
            flags.append(f"Profitable in only {pos}/{len(folds)} walk-forward folds.")
    if dev.get("total_return", 0) < dev.get("benchmark_return", 0):
        flags.append("Below buy-and-hold in development (expected for a dip-buyer in a strong bull market; "
                     "judge risk-adjusted: drawdown, Sharpe, exposure).")
    return flags or ["No red flags from these checks (still not proof of future profit)."]


def _p(v):
    return "n/a" if v is None else f"{v:+.2%}"


def _f(v):
    return "n/a" if v is None else f"{v:,.2f}"


def block(title: str, s: Optional[dict]) -> list[str]:
    if not s:
        return [f"## {title}", "- not run", ""]
    rows = [("Period", s.get("period", "n/a")), ("Net return after costs", _p(s.get("total_return"))),
            ("CAGR", _p(s.get("cagr"))), ("Max drawdown", _p(-s.get("max_drawdown", 0))),
            ("Buy-and-hold same ETF(s)", _p(s.get("benchmark_return"))),
            ("Buy-and-hold max drawdown", _p(-s.get("benchmark_max_drawdown", 0))),
            ("Sharpe / Sortino", f"{_f(s.get('sharpe'))} / {_f(s.get('sortino'))}"),
            ("Trades", s.get("trades", 0)),
            ("Win rate", "n/a" if s.get("win_rate") is None else f"{s['win_rate']:.0%}"),
            ("Avg win / avg loss", f"Rs{s.get('avg_win', 0):,.0f} / Rs{s.get('avg_loss', 0):,.0f}"),
            ("Expectancy", f"{_f(s.get('expectancy_r'))}R, Rs{s.get('expectancy_rs', 0):,.0f}/trade"),
            ("Profit factor", _f(s.get("profit_factor"))), ("Avg holding (calendar days)", _f(s.get("avg_hold_days"))),
            ("Costs paid", f"Rs{s.get('total_costs', 0):,.0f}"),
            ("Capital utilisation (avg invested)", "n/a" if s.get("exposure") is None else f"{s['exposure']:.0%}"),
            ("Tranches per trade", _f(s.get("tranches_per_trade")))]
    out = [f"## {title}", "| Metric | Value |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]
    if s.get("by_symbol"):
        out += ["", "| ETF | Trades | Win rate | Net P&L |", "|---|---|---|---|"]
        out += [f"| {k} | {v['trades']} | {v['win_rate']:.0%} | Rs{v['net']:,.0f} |" for k, v in s["by_symbol"].items()]
    if s.get("by_regime"):
        out += ["", "| Regime at entry | Trades | Win rate | Net P&L | Avg R |", "|---|---|---|---|---|"]
        out += [f"| {k} | {v['trades']} | {v['win_rate']:.0%} | Rs{v['net']:,.0f} | {_f(v['avg_r'])} |"
                for k, v in s["by_regime"].items()]
    if s.get("exit_reasons"):
        out += ["", "Exit reasons: " + ", ".join(f"{k} {v}" for k, v in s["exit_reasons"].items())]
    return out + [""]


def render(sections: list[tuple[str, dict, Optional[dict], list]], sens: dict, capital: float) -> str:
    L = ["# GoldBEES / SilverBEES backtest", "",
         f"_Capital Rs{capital:,.0f}. Daily candles, next-session limit fills, slippage+spread and Groww delivery "
         "charges (no STT on gold/silver ETFs; placeholder rates, verify). Income tax on gains is NOT deducted "
         "(short-term gains on these ETFs are taxed at your slab rate)._", "",
         "_Not modelled on daily bars: the 1-hour / 15-minute entry confirmation and intraday falls from the "
         "day's high used by the live monitor. Past results do not predict future returns._", ""]
    for name, dev, test, folds in sections:
        L += [f"# {name}"]
        L += block("Development period", dev)
        L += block("Untouched holdout period", test)
        if folds:
            L += ["## Walk-forward folds (fresh capital each)", "| Fold | Return | Buy&hold | Max DD | Trades | PF |",
                  "|---|---|---|---|---|---|"]
            L += [f"| {f['fold']} | {_p(f.get('total_return'))} | {_p(f.get('benchmark_return'))} | "
                  f"{_p(-f.get('max_drawdown', 0))} | {f.get('trades', 0)} | {_f(f.get('profit_factor'))} |"
                  for f in folds] + [""]
        L += ["## Red flags"] + [f"- {x}" for x in verdict(dev, test, folds)] + [""]
    if sens:
        L += ["# Sensitivity (combined, development period)", "| Variant | Return | Max DD | Trades | PF | Sharpe |",
              "|---|---|---|---|---|---|"]
        L += [f"| {k} | {_p(v.get('total_return'))} | {_p(-v.get('max_drawdown', 0))} | {v.get('trades', 0)} | "
              f"{_f(v.get('profit_factor'))} | {_f(v.get('sharpe'))} |" for k, v in sens.items()] + [""]
    L += ["Do not tune rules on the holdout period; once you change something after looking at it, it is no "
          "longer out-of-sample."]
    return "\n".join(L) + "\n"
