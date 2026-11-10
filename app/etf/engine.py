"""End-of-day decisions (shared by backtest and paper/live) and the daily-bar session model used by the backtest.

Day D:
  session D  : exits first (gap-aware stop / sudden drop / target / flagged exits), then planned buys fill
               (the backtest models this on the daily bar; paper/live do it with real quotes, see intraday.py)
  close of D : analyse (bars <= D), review positions, rank GoldBEES vs SilverBEES, size tranches,
               plan limit orders for session D+1. Nothing decided at D can fill at D.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from app.etf.analysis import InsufficientData, Snapshot, analyse
from app.etf.config import EtfConfig
from app.etf.ledger import EtfLedger
from app.etf.strategy import EntrySignal, Rejection, entry_signal, rank, review, size, size_core, sudden_drop_level
from app.execution.executor import make_reference_id
from app.paper.simulator import Bar
from app.risk.costs import order_charges

log = logging.getLogger(__name__)


@dataclass
class EodResult:
    d: date
    snapshots: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)          # symbol -> EntrySignal (before sizing)
    new_orders: list = field(default_factory=list)
    exits_flagged: dict = field(default_factory=dict)    # symbol -> reason (sell next session)
    rejections: dict = field(default_factory=dict)
    position_notes: dict = field(default_factory=dict)
    risk_block: Optional[str] = None
    data_issues: dict = field(default_factory=dict)
    equity: float = 0.0
    cash: float = 0.0
    unsettled: float = 0.0
    notes: list = field(default_factory=list)


def buy_ref(symbol: str, tranche: int, d: date) -> str:
    return make_reference_id(symbol, f"etfT{tranche}", d, prefix="EB")


def sell_ref(symbol: str, reason: str, d: date, n: int = 0) -> str:
    return make_reference_id(symbol, f"etf-{reason}-{n}", d, prefix="ES")


def _cooldown_active(ledger: EtfLedger, symbol: str, d: date) -> bool:
    until = ledger.get(f"cooldown_{symbol}")
    return bool(until) and date.fromisoformat(until) >= d


def start_cooldown(ledger: EtfLedger, symbol: str, d: date, sessions: int) -> None:
    ledger.set(f"cooldown_{symbol}", date.fromordinal(d.toordinal() + int(sessions * 7 / 5) + 1).isoformat())


def risk_block(ledger: EtfLedger, cfg: EtfConfig, equity: float) -> Optional[str]:
    day_start = ledger.getf("day_start_equity", equity)
    peak = max(ledger.getf("peak_equity", equity), equity)
    if day_start > 0 and (day_start - equity) / day_start * 100 >= cfg.daily_loss_limit_pct:
        return f"daily loss limit {cfg.daily_loss_limit_pct}% reached"
    if peak > 0 and (peak - equity) / peak * 100 >= cfg.max_drawdown_pct:
        return f"portfolio drawdown {(peak - equity) / peak:.1%} >= {cfg.max_drawdown_pct}% limit"
    return None


def end_of_day(d: date, ledger: EtfLedger, frames: dict, cfg: EtfConfig, kill_switch: bool = False,
               benchmark: Optional[float] = None) -> EodResult:
    """frames: symbol -> daily DataFrame ending at d (completed session)."""
    res = EodResult(d, notes=[f"strategy {cfg.strategy_mode}"])
    for sym, df in frames.items():
        if df is None or df.empty or df.index[-1] != d:
            res.data_issues[sym] = f"no completed daily candle for {d}"
            continue
        try:
            res.snapshots[sym] = analyse(sym, df)
        except InsufficientData as e:
            res.data_issues[sym] = str(e)

    # expire yesterday's unfilled plans (a plan is valid for one session only)
    for o in ledger.open_orders():
        if o["d"] < d.isoformat() and o["status"] == "PLANNED":
            ledger.update_order(o["ref"], status="EXPIRED", reason=(o["reason"] or "") + " | not filled next session")

    # ---- review open positions ----
    for p in ledger.positions():
        s = res.snapshots.get(p["symbol"])
        if s is None:
            res.position_notes[p["symbol"]] = ["no fresh candle: stop/target unchanged"]
            continue
        ledger.update_position(p["symbol"], last_close=s.close, atr_pct=s.atr_pct, sessions_held=p["sessions_held"] + 1,
                               sessions_since_tranche=p["sessions_since_tranche"] + 1,
                               peak_close=max(p["peak_close"], s.close))
        pv = ledger.view(p["symbol"])
        rv = review(pv, s, cfg.profile(p["symbol"]), cfg)
        ledger.update_position(p["symbol"], stop=rv.stop, target=rv.target, extended=int(rv.extended),
                               exit_pending=rv.exit_reason)
        res.position_notes[p["symbol"]] = rv.notes
        if rv.exit_reason:
            res.exits_flagged[p["symbol"]] = rv.exit_reason
            ledger.event(d, "EXIT_FLAGGED", p["symbol"], rv.exit_reason)

    equity = ledger.equity_now()
    res.equity, res.cash, res.unsettled = equity, ledger.getf("cash"), ledger.getf("unsettled")
    block = "kill switch active" if kill_switch else risk_block(ledger, cfg, equity)
    res.risk_block = block

    # ---- entry signals for both ETFs, then rotation ----
    sigs: list[EntrySignal] = []
    for sym, s in res.snapshots.items():
        if sym in res.exits_flagged:
            res.rejections[sym] = f"exit flagged ({res.exits_flagged[sym]})"
            continue
        out = entry_signal(s, cfg.profile(sym), cfg, ledger.view(sym), _cooldown_active(ledger, sym, d))
        if isinstance(out, Rejection):
            res.rejections[sym] = out.reason
        else:
            sigs.append(out)
            res.signals[sym] = out
    if block and sigs:
        for g in sigs:
            res.rejections[g.symbol] = f"signal ignored: {block}"
        sigs = []
    cash = ledger.available_cash(cfg.same_day_proceeds_pct)
    exposure = ledger.exposure()
    ranked = rank(sigs)
    if len(ranked) == 2:
        a, b = ranked
        res.notes.append(f"rotation: {a.symbol} score {a.score} vs {b.symbol} score {b.score}; "
                         f"{a.symbol} gets capital first")
    for g in ranked:
        s, prof = res.snapshots[g.symbol], cfg.profile(g.symbol)
        z = size(g, prof, cfg, equity, cash, exposure, ledger.view(g.symbol), s.vol_shock)
        if z.qty <= 0:
            res.rejections[g.symbol] = "risk/sizing: " + "; ".join(z.reasons)
            continue
        target = round(z.new_avg * (1 + g.targets.preferred), 2)
        ref = buy_ref(g.symbol, g.tranche, d)
        plan = {"targets": vars(g.targets), "score": g.score, "planned_value": z.planned_value,
                "net_rr": round(z.net_rr, 2), "risk": round(z.risk_amount, 2), "regime": g.regime,
                "pullback": g.pullback, "reasons": g.reasons, "atr_pct": s.atr_pct}
        if not ledger.add_order(ref, d, g.symbol, "BUY", z.qty, g.limit, stop=z.new_stop, target=target,
                                tranche=g.tranche, kind=g.kind, reason="; ".join(g.reasons[:3]), plan=plan):
            res.notes.append(f"{g.symbol}: order {ref} already planned (duplicate suppressed)")
            continue
        cost = z.qty * g.limit * (1 + prof.slippage_bps / 1e4)
        cash -= cost + order_charges(cost, "BUY", cfg.costs)
        exposure[g.symbol] = exposure.get(g.symbol, 0.0) + cost
        res.new_orders.append({"ref": ref, "symbol": g.symbol, "tranche": g.tranche, "kind": g.kind, "qty": z.qty,
                               "limit": g.limit, "stop": z.new_stop, "target": target, "score": g.score,
                               "net_rr": round(z.net_rr, 2), "risk": round(z.risk_amount, 2),
                               "reasons": g.reasons, "regime": g.regime})
        ledger.event(d, "ORDER_PLANNED", g.symbol, f"T{g.tranche} {g.kind} {z.qty} @ {g.limit:.2f} stop {z.new_stop:.2f} "
                                                   f"target {target:.2f}")

    planned_syms = {o["symbol"] for o in res.new_orders}
    if cfg.strategy_mode == "momentum" and not block:
        for sym, s in res.snapshots.items():
            if sym in res.exits_flagged or sym in planned_syms:
                continue
            prof = cfg.profile(sym)
            sig, z = size_core(sym, s, prof, cfg, equity, cash, exposure, ledger.view(sym))
            if sig is None or z.qty <= 0:
                if z.reasons and sym not in res.rejections:
                    res.rejections.setdefault(sym, "core: " + "; ".join(z.reasons))
                continue
            target = round(z.new_avg * (1 + sig.targets.preferred), 2)
            ref = buy_ref(sym, sig.tranche, d)
            plan = {"targets": vars(sig.targets), "score": sig.score, "planned_value": z.planned_value,
                    "net_rr": round(z.net_rr, 2), "risk": round(z.risk_amount, 2), "regime": sig.regime,
                    "pullback": sig.pullback, "reasons": sig.reasons, "atr_pct": s.atr_pct}
            if ledger.add_order(ref, d, sym, "BUY", z.qty, sig.limit, stop=z.new_stop, target=target,
                                tranche=sig.tranche, kind=sig.kind, reason="; ".join(sig.reasons[:2]), plan=plan):
                cost = z.qty * sig.limit * (1 + prof.slippage_bps / 1e4)
                cash -= cost + order_charges(cost, "BUY", cfg.costs)
                exposure[sym] = exposure.get(sym, 0.0) + cost
                res.new_orders.append({"ref": ref, "symbol": sym, "tranche": sig.tranche, "kind": sig.kind,
                                       "qty": z.qty, "limit": sig.limit, "stop": z.new_stop, "target": target,
                                       "score": sig.score, "net_rr": round(z.net_rr, 2),
                                       "risk": round(z.risk_amount, 2), "reasons": sig.reasons, "regime": sig.regime})
                ledger.event(d, "ORDER_PLANNED", sym, f"CORE {z.qty} @ {sig.limit:.2f}")

    # ---- bookkeeping ----
    ledger.set("peak_equity", max(ledger.getf("peak_equity", equity), equity))
    ledger.set("prev_equity", equity)
    ledger.set("day_start_equity", equity)
    ledger.record_equity(d, benchmark)
    ledger.set("last_eod", d.isoformat())
    return res


# ---------------------------------------------------------------- backtest session model (daily bars)
def _bar(df: pd.DataFrame, d: date) -> Optional[Bar]:
    if df is None or d not in df.index:
        return None
    r = df.loc[d]
    return Bar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))


def simulate_session(d: date, ledger: EtfLedger, frames: dict, cfg: EtfConfig, log_lines: Optional[list] = None) -> None:
    """Model session d on daily bars, worst case first: stop / sudden drop before target.
    Intraday falls from the day's high cannot be seen on a daily bar; only falls from the previous close are modelled."""
    out = log_lines if log_lines is not None else []
    ledger.settle(d)
    for p in ledger.positions():
        sym = p["symbol"]
        df = frames.get(sym)
        bar = _bar(df, d)
        if bar is None:
            continue
        prof = cfg.profile(sym)
        slip = prof.slippage_bps / 1e4
        qty = ledger.sellable_qty(sym, d)
        if qty <= 0:
            continue
        prev = df.loc[:d]["close"].iloc[-2] if len(df.loc[:d]) > 1 else bar.open
        drop_lvl, _ = sudden_drop_level(prev, bar.high, p["atr_pct"] or 0.01, prof)
        reason, px = None, None
        if p["exit_pending"]:
            reason, px = p["exit_pending"], bar.open
        elif bar.open <= max(p["stop"], drop_lvl):
            reason = "STOP_GAP" if bar.open <= p["stop"] else "SUDDEN_DROP_GAP"
            px = bar.open
        elif bar.low <= max(p["stop"], drop_lvl):
            if p["stop"] >= drop_lvl:
                reason, px = ("TRAIL_STOP" if p["stop"] >= p["avg"] else "STOP"), p["stop"]
            else:
                reason, px = "SUDDEN_DROP", drop_lvl
        elif bar.open >= p["target"]:
            reason, px = "TARGET_GAP", bar.open
        elif bar.high >= p["target"]:
            reason, px = ("TARGET_EXTENDED" if p["extended"] else "TARGET"), p["target"]
        if reason:
            fill = px * (1 - slip)
            cost = order_charges(fill * qty, "SELL", cfg.costs)
            net = ledger.sell(d, sym, qty, fill, cost, reason)
            out.append(f"{d} SELL {qty} {sym} @ {fill:.2f} [{reason}] net {net:+,.0f}")
            if reason.startswith(("STOP", "SUDDEN")):
                start_cooldown(ledger, sym, d, cfg.cooldown_days)
            if ledger.position(sym) is not None:
                ledger.update_position(sym, exit_pending=reason)
    for o in ledger.open_orders(side="BUY"):
        if o["d"] >= d.isoformat():
            continue
        sym = o["symbol"]
        bar = _bar(frames.get(sym), d)
        if bar is None:
            ledger.update_order(o["ref"], status="EXPIRED", reason="no candle")
            continue
        prof = cfg.profile(sym)
        if bar.open <= o["stop"]:
            ledger.update_order(o["ref"], status="CANCELLED", reason="opened at/below stop: setup invalidated")
            continue
        chase = cfg.max_chase_pct / 100
        cap = o["limit_price"] * (1 + chase) if cfg.strategy_mode != "momentum" else o["limit_price"]
        if bar.open > cap:
            ledger.update_order(o["ref"], status="EXPIRED", reason="gapped above limit: not chasing")
            continue
        if bar.open <= o["limit_price"]:
            px = bar.open
        elif bar.open <= cap:
            px = bar.open
        elif bar.low <= o["limit_price"]:
            px = o["limit_price"]
        else:
            ledger.update_order(o["ref"], status="EXPIRED", reason="limit not reached")
            continue
        fill = px * (1 + prof.slippage_bps / 1e4)
        cost = order_charges(fill * o["qty"], "BUY", cfg.costs)
        if fill * o["qty"] + cost > ledger.available_cash(cfg.same_day_proceeds_pct):
            ledger.update_order(o["ref"], status="REJECTED", reason="insufficient settled cash at fill")
            continue
        apply_buy_fill(d, ledger, o, o["qty"], fill, cost)
        out.append(f"{d} BUY T{o['tranche']} {o['qty']} {sym} @ {fill:.2f}")


def apply_buy_fill(d: date, ledger: EtfLedger, o, qty: int, fill: float, cost: float,
                   total_filled: Optional[int] = None, done: bool = True) -> None:
    """Book `qty` newly filled units of buy order `o`. Partial fills call this once per increment."""
    import json
    plan = json.loads(o["plan"] or "{}")
    tg = plan.get("targets", {})
    p = ledger.position(o["symbol"])
    avg = fill if p is None else (p["qty"] * p["avg"] + qty * fill) / (p["qty"] + qty)
    target = round(avg * (1 + tg.get("preferred", 0.02)), 2)
    ledger.buy_fill(d, o["symbol"], qty, fill, cost, tranche=o["tranche"], kind=o["kind"], stop=o["stop"],
                    target=target, planned_value=plan.get("planned_value", qty * fill), regime=plan.get("regime", ""),
                    reason=o["reason"] or "", same_order=(o["filled_qty"] or 0) > 0)
    if p is None:
        ledger.update_position(o["symbol"], atr_pct=plan.get("atr_pct", 0.01))
    prev_q, prev_avg = o["filled_qty"] or 0, o["avg_fill"] or 0.0
    n = total_filled if total_filled is not None else prev_q + qty
    avg_fill = (prev_q * prev_avg + qty * fill) / n if n else fill
    ledger.update_order(o["ref"], status="FILLED" if done else "PARTIAL", filled_qty=n, avg_fill=avg_fill)
