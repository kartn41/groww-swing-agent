"""Backtesting with the SAME engine as paper trading (app.paper.engine.process_day).

What it reports (spec section 15): return, CAGR, max drawdown, Sharpe, Sortino, win rate, average
win/loss, expectancy (R), profit factor, holding period, costs/turnover, exposure, results per
strategy and per market regime, trade count with a bootstrap confidence interval, and NIFTY
buy-and-hold over the same days. Validation: development vs untouched holdout period, walk-forward
folds, risk/execution sensitivity and per-strategy ablation.

Known bias that cannot be removed with current data: the universe is TODAY's NIFTY 50 list, so
stocks that were removed from the index (often after falling) are missing => survivorship bias,
results are optimistic. This is printed in every report.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from app.config import RiskLimits, Settings
from app.paper.engine import INDEX_SYMBOL, process_day
from app.paper.ledger import Ledger

SURVIVORSHIP_NOTE = ("Universe = current NIFTY 50 constituents applied to the past (survivorship bias: "
                     "results are optimistic).")


class CachedStore:
    """Loads every symbol once; returns only the last `tail` bars up to `end` (enough for SMA200 etc.)."""

    def __init__(self, store, symbols: list[str], tail: int = 420):
        self._frames = {s: store.load(s) for s in symbols}
        self.tail = tail

    def load(self, symbol: str, end: Optional[date] = None) -> pd.DataFrame:
        df = self._frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if end is not None:
            df = df.loc[:end]
        return df.iloc[-self.tail:]

    def trading_days(self, start: date, end: date) -> list[date]:
        idx = self._frames.get(INDEX_SYMBOL)
        return [] if idx is None else [d for d in idx.index if start <= d <= end]


def run(cache: CachedStore, universe, start: date, end: date, settings: Settings, capital: float,
        allowed_strategies=None, events=None) -> Ledger:
    led = Ledger(":memory:", capital)
    for d in cache.trading_days(start, end):
        process_day(d, led, cache, universe, settings, events=events, allowed_strategies=allowed_strategies)
    return led


def _max_dd(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float((1 - series / series.cummax()).max())


def stats(led: Ledger, seed: int = 0) -> dict:
    eq = pd.read_sql_query("SELECT d, equity, benchmark, invested FROM equity ORDER BY d", led.con)
    tr = pd.read_sql_query("SELECT * FROM trades ORDER BY id", led.con)
    start = led.getf("start_capital")
    out: dict = {"days": len(eq), "trades": len(tr)}
    if eq.empty:
        return out
    d0, d1 = date.fromisoformat(eq["d"].iloc[0]), date.fromisoformat(eq["d"].iloc[-1])
    years = max((d1 - d0).days, 1) / 365.25
    final = float(eq["equity"].iloc[-1])
    rets = eq["equity"].pct_change().dropna()
    downside = rets[rets < 0]
    out.update({
        "period": f"{d0} to {d1}",
        "total_return": final / start - 1,
        "cagr": (final / start) ** (1 / years) - 1 if years >= 0.25 else None,
        "max_drawdown": _max_dd(eq["equity"]),
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else None,
        "sortino": float(rets.mean() / downside.std() * math.sqrt(252)) if len(downside) > 1 and downside.std() > 0 else None,
        "exposure": float((eq["invested"].fillna(0) / eq["equity"]).mean()),
        "benchmark_return": float(eq["benchmark"].iloc[-1] / eq["benchmark"].iloc[0] - 1),
        "benchmark_max_drawdown": _max_dd(eq["benchmark"]),
    })
    if tr.empty:
        return out
    r_mult = tr["net"] / tr["risk"].where(tr["risk"] > 0)
    wins, losses = tr[tr["net"] > 0], tr[tr["net"] <= 0]
    hold = (pd.to_datetime(tr["exit_date"]) - pd.to_datetime(tr["entry_date"])).dt.days
    rng = np.random.default_rng(seed)
    rv = r_mult.dropna().to_numpy()
    ci = None
    if len(rv) >= 5:
        boots = rng.choice(rv, size=(2000, len(rv)), replace=True).mean(axis=1)
        ci = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))
    gross_win, gross_loss = float(wins["net"].sum()), float(-losses["net"].sum())
    traded_value = float((tr["entry"] * tr["qty"] + tr["exit"] * tr["qty"]).sum())
    out.update({
        "win_rate": len(wins) / len(tr),
        "avg_win": float(wins["net"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["net"].mean()) if len(losses) else 0.0,
        "expectancy_rs": float(tr["net"].mean()),
        "expectancy_r": float(r_mult.mean()) if r_mult.notna().any() else None,
        "expectancy_r_ci95": ci,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_hold_days": float(hold.mean()),
        "total_costs": float(tr["costs"].sum()),
        "turnover": traded_value / start,
        "by_strategy": _group(tr, r_mult, "strategy"),
        "by_regime": _group(tr, r_mult, "regime"),
        "exit_reasons": tr["reason"].value_counts().to_dict(),
    })
    return out


def _group(tr: pd.DataFrame, r_mult: pd.Series, col: str) -> dict:
    g = {}
    for k, sub in tr.groupby(tr[col].fillna("unknown")):
        rm = r_mult.loc[sub.index]
        g[k] = {"trades": len(sub), "win_rate": float((sub["net"] > 0).mean()),
                "net": float(sub["net"].sum()), "avg_r": float(rm.mean()) if rm.notna().any() else None}
    return g


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, 28))


def walk_forward(cache, universe, start: date, end: date, settings, capital, months: int = 6, events=None):
    """Consecutive out-of-sample folds, each starting flat with fresh capital. Strategy rules are fixed
    (not re-fitted per fold), so this measures stability across market periods."""
    folds, s = [], start
    while s < end:
        e = min(add_months(s, months), end)
        led = run(cache, universe, s, e, settings, capital, events=events)
        st = stats(led)
        st["fold"] = f"{s} to {e}"
        folds.append(st)
        s = date.fromordinal(e.toordinal() + 1)            # next fold starts the day after: no shared day
    return folds


SENSITIVITY = {
    "baseline": {},
    "min R:R 1.8": {"min_reward_risk": 1.8},
    "min R:R 2.5": {"min_reward_risk": 2.5},
    "risk/trade 0.25%": {"risk_per_trade_pct": 0.25},
    "risk/trade 1.0%": {"risk_per_trade_pct": 1.0},
    "slippage x3 (30 bps)": {"slippage_bps": 30.0},
    "chase 0.25%": {"max_chase_pct": 0.25},
}


def sensitivity(cache, universe, start, end, settings: Settings, capital, events=None) -> dict:
    out = {}
    for name, kw in SENSITIVITY.items():
        s2 = replace(settings, risk=replace(settings.risk, **kw))
        out[name] = stats(run(cache, universe, start, end, s2, capital, events=events))
    return out


def ablation(cache, universe, start, end, settings, capital, names, events=None) -> dict:
    return {n: stats(run(cache, universe, start, end, settings, capital, allowed_strategies=[n], events=events))
            for n in names}


def verdict(dev: dict, test: Optional[dict], folds: list[dict]) -> list[str]:
    """Heuristic red flags. Passing these is necessary, not sufficient."""
    flags = []
    n = dev.get("trades", 0)
    if n < 30:
        flags.append(f"Only {n} trades in development: too few to conclude anything.")
    ci = dev.get("expectancy_r_ci95")
    if ci and ci[0] <= 0:
        flags.append(f"Expectancy 95% interval includes zero or below ({ci[0]:+.2f}R to {ci[1]:+.2f}R).")
    pf = dev.get("profit_factor")
    if pf is not None and pf < 1.2:
        flags.append(f"Profit factor {pf:.2f} < 1.2.")
    if dev.get("total_return", 0) <= dev.get("benchmark_return", 0):
        flags.append("Did not beat NIFTY buy-and-hold in development.")
    if test is not None:
        if test.get("total_return", 0) <= 0:
            flags.append("Lost money in the untouched holdout period.")
        if test.get("total_return", 0) <= test.get("benchmark_return", 0):
            flags.append("Did not beat NIFTY buy-and-hold in the holdout period.")
    if folds:
        pos = sum(1 for f in folds if f.get("total_return", 0) > 0)
        if pos / len(folds) < 0.6:
            flags.append(f"Profitable in only {pos}/{len(folds)} walk-forward folds.")
    return flags or ["No red flags from these checks (still not proof of future profit)."]


def _fmt(v, pct=False, r=False):
    if v is None:
        return "n/a"
    if isinstance(v, tuple):
        return f"{v[0]:+.2f}R to {v[1]:+.2f}R"
    if pct:
        return f"{v:+.2%}"
    if r:
        return f"{v:+.2f}R"
    return f"{v:,.2f}" if isinstance(v, float) else str(v)


def _rs(v):
    return "n/a" if v is None else f"Rs{v:,.0f}"


def _pct(v):
    return "n/a" if v is None else f"{v:.1%}"


def render(dev: dict, test: Optional[dict], folds, sens: dict, abl: dict, capital: float) -> str:
    L = ["# Backtest report", "", f"_{SURVIVORSHIP_NOTE}_", "",
         "_Simulated on daily candles with conservative fills, placeholder cost rates, and the same rules as paper "
         "trading. Past results do not predict future returns._", "",
         "_Historical results dates are not available, so the backtest does NOT avoid entries before past "
         "earnings announcements as live/paper trading does; gaps on results days are included unfiltered._", ""]

    def block(title, s):
        if not s:
            return [f"## {title}", "- not run", ""]
        rows = [("Period", s.get("period", "n/a")), ("Total return", _fmt(s.get("total_return"), pct=True)),
                ("CAGR", _fmt(s.get("cagr"), pct=True)), ("Max drawdown", _fmt(-s.get("max_drawdown", 0), pct=True)),
                ("NIFTY buy-and-hold", _fmt(s.get("benchmark_return"), pct=True)),
                ("NIFTY max drawdown", _fmt(-s.get("benchmark_max_drawdown", 0), pct=True)),
                ("Sharpe / Sortino", f"{_fmt(s.get('sharpe'))} / {_fmt(s.get('sortino'))}"),
                ("Trades", s.get("trades", 0)), ("Win rate", _pct(s.get("win_rate"))),
                ("Avg win / avg loss", f"{_rs(s.get('avg_win'))} / {_rs(s.get('avg_loss'))}"),
                ("Expectancy", f"{_fmt(s.get('expectancy_r'), r=True)} (95% CI {_fmt(s.get('expectancy_r_ci95'))})"),
                ("Profit factor", _fmt(s.get("profit_factor"))), ("Avg holding (days)", _fmt(s.get("avg_hold_days"))),
                ("Costs paid", _rs(s.get("total_costs"))), ("Turnover (x capital)", _fmt(s.get("turnover"))),
                ("Exposure (avg invested)", _pct(s.get("exposure")))]
        out = [f"## {title}", "| Metric | Value |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]
        for key, label in (("by_strategy", "Strategy"), ("by_regime", "Regime at entry")):
            if s.get(key):
                out += ["", f"| {label} | Trades | Win rate | Net P&L | Avg R |", "|---|---|---|---|---|"]
                out += [f"| {k} | {v['trades']} | {v['win_rate']:.0%} | Rs{v['net']:,.0f} | {_fmt(v['avg_r'], r=True)} |"
                        for k, v in s[key].items()]
        if s.get("exit_reasons"):
            out += ["", "Exit reasons: " + ", ".join(f"{k} {v}" for k, v in s["exit_reasons"].items())]
        return out + [""]

    L += block("Development period", dev)
    L += block("Untouched holdout period", test)
    if folds:
        L += ["## Walk-forward folds (fresh capital each)", "| Fold | Return | NIFTY | Max DD | Trades | Expectancy |",
              "|---|---|---|---|---|---|"]
        L += [f"| {f['fold']} | {_fmt(f.get('total_return'), pct=True)} | {_fmt(f.get('benchmark_return'), pct=True)} | "
              f"{_fmt(-f.get('max_drawdown', 0), pct=True)} | {f.get('trades', 0)} | {_fmt(f.get('expectancy_r'), r=True)} |"
              for f in folds] + [""]
    if sens:
        L += ["## Sensitivity (development period)", "| Variant | Return | Max DD | Trades | Expectancy |",
              "|---|---|---|---|---|"]
        L += [f"| {k} | {_fmt(v.get('total_return'), pct=True)} | {_fmt(-v.get('max_drawdown', 0), pct=True)} | "
              f"{v.get('trades', 0)} | {_fmt(v.get('expectancy_r'), r=True)} |" for k, v in sens.items()] + [""]
    if abl:
        L += ["## Each strategy alone (development period)", "| Strategy | Return | Trades | Win rate | Expectancy |",
              "|---|---|---|---|---|"]
        L += [f"| {k} | {_fmt(v.get('total_return'), pct=True)} | {v.get('trades', 0)} | "
              f"{_fmt(v.get('win_rate'), pct=True)} | {_fmt(v.get('expectancy_r'), r=True)} |" for k, v in abl.items()] + [""]
    L += ["## Red flags (heuristic checks)"] + [f"- {x}" for x in verdict(dev, test, folds)]
    L += ["", "Do not tune rules on the holdout period; once you look at it and change something, it is no "
              "longer untouched."]
    return "\n".join(L) + "\n"
