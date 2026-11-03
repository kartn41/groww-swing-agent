"""One paper-trading day, decided on the close of day D, executed on later bars.

Order of work for day D:
  1. fills of orders created BEFORE D, using bar D        (orders never see their own decision bar)
  2. exits of positions on bar D (gap-aware, stop-first)
  3. mark to D close; flag trend breaks for exit at the next open
  4. regime + candidates + risk engine on data <= D  ->  orders for the next session
The whole day commits atomically; a crash mid-day leaves the ledger as it was.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
import pandas as pd

from app.analysis.indicators import roc, sma
from app.analysis.regime import breadth, classify
from app.config import Settings
from app.data.quality import validate_daily
from app.paper.ledger import Ledger
from app.paper.simulator import Bar, entry_fill, exit_check
from app.risk.costs import order_charges
from app.risk.engine import OpenPosition, PortfolioState, TradePlan, evaluate as risk_evaluate
from app.events import EventCalendar
from app.strategies import STRATEGIES, permitted
from app.strategies.common import Frame, Rejection
from app.universe import Constituent

log = logging.getLogger(__name__)
INDEX_SYMBOL = "NSE-NIFTY"
REGIME_POINTS = {"STRONG_UP": 15.0, "WEAK_UP": 8.0}


def gsym(symbol: str) -> str:
    return f"NSE-{symbol}"


@dataclass
class DayResult:
    d: date
    regime: str = "UNKNOWN"
    regime_detail: str = ""
    fills: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    new_orders: list[dict] = field(default_factory=list)
    rejections: dict[str, str] = field(default_factory=dict)
    risk_rejections: dict[str, str] = field(default_factory=dict)
    data_issues: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    event_warnings: list[str] = field(default_factory=list)
    equity: float = 0.0
    cash: float = 0.0
    benchmark: Optional[float] = None
    skipped: bool = False


def _bar(df: pd.DataFrame, d: date) -> Optional[Bar]:
    if d not in df.index:
        return None
    r = df.loc[d]
    return Bar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))


def process_day(d: date, ledger: Ledger, store, universe: list[Constituent], settings: Settings,
                kill_switch: bool = False, events: Optional[EventCalendar] = None,
                events_required: bool = False, event_block_days: int = 7,
                allowed_strategies: Optional[list[str]] = None) -> DayResult:
    res = DayResult(d)
    index_df = store.load(INDEX_SYMBOL, end=d)
    if index_df.empty or index_df.index[-1] != d:
        res.skipped = True
        res.notes.append(f"no NIFTY candle for {d}: market closed or data not yet available")
        return res

    ledger.begin_day(d)
    try:
        _process(d, ledger, store, universe, settings, kill_switch, index_df, res,
                 events, events_required, event_block_days, allowed_strategies)
        ledger.end_day(d)                       # commit
    except Exception:
        ledger.con.rollback()
        raise
    return res


def _process(d, ledger: Ledger, store, universe, settings: Settings, kill_switch, index_df, res: DayResult,
             events=None, events_required=False, event_block_days=7, allowed_strategies=None):
    lim, costs = settings.risk, settings.costs
    slip = lim.slippage_bps / 10_000
    frames: dict[str, pd.DataFrame] = {}
    quality_ok: dict[str, bool] = {}
    for c in universe:
        df = store.load(gsym(c.symbol), end=d)
        frames[c.symbol] = df
        q = validate_daily(df, d)
        quality_ok[c.symbol] = q.ok
        if not q.ok:
            res.data_issues[c.symbol] = "; ".join(q.issues)
    uni_syms = {c.symbol for c in universe}
    for p in ledger.positions():
        if p["symbol"] not in uni_syms:          # dropped from NIFTY 50: keep managing exits, no new entries
            frames[p["symbol"]] = store.load(gsym(p["symbol"]), end=d)
            res.notes.append(f"{p['symbol']}: no longer in the NIFTY 50 list; still managed for exits")
    index_q = validate_daily(index_df, d)
    if not index_q.ok:
        res.data_issues[INDEX_SYMBOL] = "; ".join(index_q.issues)

    # week bookkeeping (for the weekly drawdown limit)
    wk = "%d-W%02d" % d.isocalendar()[:2]
    if ledger.get("week_key") != wk:
        ledger.set("week_key", wk)
        ledger.set("week_start_equity", ledger.getf("prev_equity"))

    # ---- 1+2. exits first (frees cash), then fills ----
    for p in ledger.positions():
        df = frames.get(p["symbol"])
        bar = _bar(df, d) if df is not None else None
        if bar is None:
            res.notes.append(f"{p['symbol']}: no candle for {d}; position carried, unverified")
            continue
        if p["exit_pending"]:
            reason, px = "TREND_BREAK", bar.open
        else:
            reason, px = exit_check(bar, p["stop"], p["target"], entry_day=False)
        if reason:
            _sell(d, ledger, p, px * (1 - slip), reason, costs, res)
    for o in ledger.pending_orders(before=d):
        df = frames.get(o["symbol"])
        bar = _bar(df, d) if df is not None else None
        if bar is None:
            ledger.close_order(o["id"], "EXPIRED", "no candle for fill session")
            continue
        px, note = entry_fill(bar, o["limit_price"], lim.max_chase_pct)
        if px is None:
            ledger.close_order(o["id"], "EXPIRED", note)
            ledger.event(d, "ORDER_EXPIRED", o["symbol"], note)
            continue
        fill = px * (1 + slip)
        buy_cost = order_charges(fill * o["qty"], "BUY", costs)
        if fill * o["qty"] + buy_cost > ledger.getf("cash"):
            ledger.close_order(o["id"], "REJECTED", "insufficient paper cash at fill")
            continue
        ledger.set("cash", ledger.getf("cash") - fill * o["qty"] - buy_cost)
        ledger.open_position(d, o, fill, buy_cost)
        ledger.close_order(o["id"], "FILLED", note)
        res.fills.append(f"BUY {o['qty']} {o['symbol']} @ {fill:.2f} ({note}); stop {o['stop']:.2f}, target {o['target']:.2f}")
        ledger.event(d, "FILL", o["symbol"], res.fills[-1])
        reason, xpx = exit_check(bar, o["stop"], o["target"], entry_day=True)
        if reason:
            p = next(p for p in ledger.positions() if p["symbol"] == o["symbol"])
            _sell(d, ledger, p, xpx * (1 - slip), reason + "_ENTRY_DAY", costs, res)

    # ---- 3. mark to close, flag trend breaks ----
    for p in ledger.positions():
        df = frames.get(p["symbol"])
        if df is None or d not in df.index:
            continue
        close = float(df.loc[d, "close"])
        m50 = sma(df["close"], 50).iloc[-1]
        broken = bool(close < m50)
        ledger.mark(p["symbol"], close, exit_pending=broken)
        if broken:
            res.notes.append(f"{p['symbol']}: close {close:.2f} < SMA50 {m50:.2f}; exit at next open")

    equity = ledger.equity_now()
    day_change = equity - ledger.getf("prev_equity")

    # ---- 4. regime, candidates, risk ----
    regime = classify(index_df if index_q.ok else None,
                      breadth({c.symbol: frames[c.symbol] for c in universe if quality_ok[c.symbol]}))
    res.regime, res.regime_detail = regime.name, regime.detail
    held = {p["symbol"] for p in ledger.positions()}
    names = permitted(regime.name, allowed_strategies)
    events_ok = events is not None and events.usable
    if events is not None:
        res.notes.extend(events.warnings)
    for p in ledger.positions():                       # warn (never force-sell) on upcoming events
        if events is not None:
            for e in events.upcoming(p["symbol"], d, event_block_days):
                res.event_warnings.append(f"{p['symbol']} (held): {e.purpose or 'event'} on {e.d}")
    if kill_switch:
        res.notes.append("KILL SWITCH active: no new orders (exits still managed)")
    elif not names:
        res.notes.append(f"regime {regime.name}: no strategy permitted, no new long entries")
    elif events_required and not events_ok:
        res.notes.append("event calendar missing or stale and required: no new entries")
    else:
        idx63 = float(roc(index_df["close"], 63).iloc[-1])
        sec_rets: dict[str, list[float]] = {}
        for c in universe:
            f = frames[c.symbol]
            if quality_ok[c.symbol] and len(f) > 64:
                sec_rets.setdefault(c.sector, []).append(float(roc(f["close"], 63).iloc[-1]))
        sector_rs = {k: sum(v) / len(v) - idx63 for k, v in sec_rets.items() if v}
        setups = []
        for c in universe:
            if c.symbol in held:
                continue
            if not quality_ok[c.symbol]:
                res.rejections[c.symbol] = "data quality: " + res.data_issues.get(c.symbol, "")
                continue
            if events is not None:
                ev = events.blocking_event(c.symbol, d, event_block_days)
                if ev is not None:
                    res.rejections[c.symbol] = f"event risk: {ev.purpose or 'corporate event'} on {ev.d}"
                    continue
            if len(frames[c.symbol]) < 220:
                res.rejections[c.symbol] = "insufficient history"
                continue
            fr = Frame(frames[c.symbol], index_df)
            best, reasons = None, []
            for n in names:
                out = STRATEGIES[n](c.symbol, c.sector, frames[c.symbol], index_df,
                                    REGIME_POINTS.get(regime.name, 0.0), frame=fr, sector_rs=sector_rs.get(c.sector))
                if isinstance(out, Rejection):
                    reasons.append(f"{n}: {out.reason}")
                else:
                    out.strategy = n
                    if not events_ok:
                        out.evidence.append("event calendar unavailable: check results dates manually")
                    if best is None or out.score > best.score:
                        best = out
            if best is None:
                res.rejections[c.symbol] = " | ".join(reasons)
            else:
                setups.append(best)
        setups.sort(key=lambda s: s.score, reverse=True)

        positions = [OpenPosition(p["symbol"], p["sector"], p["qty"], p["entry"], p["stop"])
                     for p in ledger.positions()]
        cash = ledger.getf("cash")
        for s in setups:
            df = frames[s.symbol]
            adv = float((df["close"] * df["volume"]).iloc[-20:].mean())
            state = PortfolioState(equity=equity, cash=cash, positions=list(positions),
                                   realized_pnl_today=min(day_change, 0.0),
                                   week_start_equity=float(ledger.get("week_start_equity") or equity),
                                   peak_equity=ledger.getf("peak_equity"), data_ok=index_q.ok)
            dec = risk_evaluate(TradePlan(s.symbol, s.sector, s.entry, s.stop, s.target), state, lim, costs, adv)
            if not dec.approved:
                res.risk_rejections[s.symbol] = f"{s.strategy}: " + "; ".join(dec.reasons)
                ledger.event(d, "RISK_REJECTED", s.symbol, res.risk_rejections[s.symbol])
                continue
            ledger.add_order(d, s, dec.qty, regime.name)
            positions.append(OpenPosition(s.symbol, s.sector, dec.qty, s.entry, s.stop))
            cash -= dec.qty * s.entry * (1 + slip) + order_charges(dec.qty * s.entry, "BUY", costs)
            res.new_orders.append({"symbol": s.symbol, "strategy": s.strategy, "qty": dec.qty, "entry": s.entry,
                                   "stop": s.stop, "target": s.target, "score": s.score,
                                   "net_rr": round(dec.net_reward_risk, 2), "evidence": s.evidence,
                                   "invalidation": s.invalidation})
            ledger.event(d, "ORDER_PLANNED", s.symbol,
                         f"{s.strategy} qty {dec.qty} entry {s.entry} stop {s.stop} target {s.target}")

    for sym, why in res.rejections.items():
        ledger.event(d, "REJECTED", sym, why)

    # ---- bookkeeping ----
    res.equity, res.cash = equity, ledger.getf("cash")
    res.benchmark = float(index_df["close"].iloc[-1])
    if ledger.get("benchmark_start") is None:
        ledger.set("benchmark_start", res.benchmark)
    ledger.set("peak_equity", max(ledger.getf("peak_equity"), equity))
    ledger.set("prev_equity", equity)
    ledger.record_equity(d, res.benchmark)


def _sell(d, ledger: Ledger, p, price: float, reason: str, costs, res: DayResult) -> None:
    sell_cost = order_charges(price * p["qty"], "SELL", costs)
    net = ledger.close_position(d, p, price, reason, sell_cost)
    ledger.set("cash", ledger.getf("cash") + price * p["qty"] - sell_cost)
    res.exits.append(f"SELL {p['qty']} {p['symbol']} @ {price:.2f} [{reason}] net P&L Rs{net:,.0f}")
    ledger.event(d, "EXIT", p["symbol"], res.exits[-1])
