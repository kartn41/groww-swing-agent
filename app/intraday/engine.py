"""Process one session bar-by-bar on 5m data (paper MIS-style, long-only ORB)."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional
import pandas as pd

from app.config import Settings
from app.data.intraday_store import IntradayStore
from app.intraday.config import IntradayConfig, load_intraday_config
from app.intraday.orb import NAME as ORB_NAME, evaluate_orb, opening_range
from app.paper.ledger import Ledger
from app.paper.simulator import Bar, exit_check
from app.risk.costs import order_charges
from app.universe import Constituent

log = logging.getLogger(__name__)
INDEX_SYMBOL = "NSE-NIFTY"


def gsym(symbol: str) -> str:
    return f"NSE-{symbol}"


@dataclass
class SessionResult:
    d: date
    fills: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    rejections: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    equity: float = 0.0
    cash: float = 0.0
    benchmark: Optional[float] = None
    session_start_equity: float = 0.0
    session_pnl: float = 0.0
    session_pnl_pct: float = 0.0
    new_entries: list[dict] = field(default_factory=list)
    skipped: bool = False


def _bar_row(row) -> Bar:
    return Bar(float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))


def _session_bars(store: IntradayStore, day: date) -> list[datetime]:
    ts = store.session_times(day, INDEX_SYMBOL)
    if not ts:
        ts = store.session_times(day, gsym("RELIANCE"))
    return ts


def _qty(equity: float, entry: float, stop: float, cfg: IntradayConfig) -> int:
    risk_amt = equity * cfg.risk_per_trade_pct / 100
    per_share = max(entry - stop, 0.01)
    q = int(risk_amt // per_share)
    max_val = equity * 0.95
    if q * entry > max_val:
        q = int(max_val // entry)
    return max(q, 0)


def process_session(
    day: date,
    ledger: Ledger,
    store: IntradayStore,
    universe: list[Constituent],
    settings: Settings,
    icfg: Optional[IntradayConfig] = None,
) -> SessionResult:
    icfg = icfg or load_intraday_config()
    res = SessionResult(day)
    last = ledger.get("last_processed")
    if last and date.fromisoformat(last) >= day:
        res.skipped = True
        res.notes.append(f"{day} already processed (last {last})")
        return res

    times = _session_bars(store, day)
    if len(times) < 10:
        res.skipped = True
        res.notes.append(f"insufficient 5m bars for {day}")
        return res

    index_full = store.load(INDEX_SYMBOL, day=day)
    if index_full.empty:
        res.skipped = True
        res.notes.append(f"no index 5m data for {day}")
        return res

    day_start_equity = ledger.equity_now()
    ledger.set("session_start_equity", day_start_equity)
    res.session_start_equity = day_start_equity
    trades_today = 0
    if ledger.get("last_intraday_day") != day.isoformat():
        ledger.set("last_intraday_day", day.isoformat())
        ledger.set("last_intraday_ts", "")
    last_ts_key = ledger.get("last_intraday_ts") or ""

    bench: Optional[float] = None
    try:
        for ts in times:
            ts_key = ts.strftime("%Y-%m-%d %H:%M:%S")
            if last_ts_key and ts_key <= last_ts_key:
                continue

            t = ts.time()
            index_upto = index_full.loc[:ts]
            or_ready = t > icfg.or_end

            # ---- exits ----
            for p in list(ledger.positions()):
                sym = p["symbol"]
                df = store.load(gsym(sym), day=day, end_ts=ts_key)
                if df.empty or ts not in df.index:
                    continue
                bar = _bar_row(df.loc[ts])
                entry_day = p["entry_date"] == day.isoformat()
                reason, px = exit_check(bar, p["stop"], p["target"], entry_day=False)
                if reason and px is not None:
                    slip = settings.risk.slippage_bps / 10_000
                    sell = px * (1 - slip)
                    sc = order_charges(sell * p["qty"], "SELL", settings.costs)
                    net = ledger.close_position(day, p, sell, f"INTRADAY_{reason}", sc)
                    res.exits.append(f"{sym} {reason} @ {sell:.2f} net {net:+.0f}")
                    ledger.event(day, "EXIT", sym, reason)
                    continue
                if t >= icfg.flat_by:
                    slip = settings.risk.slippage_bps / 10_000
                    sell = bar.close * (1 - slip)
                    sc = order_charges(sell * p["qty"], "SELL", settings.costs)
                    net = ledger.close_position(day, p, sell, "INTRADAY_EOD", sc)
                    res.exits.append(f"{sym} EOD flat @ {sell:.2f} net {net:+.0f}")
                    ledger.event(day, "EXIT", sym, "EOD")

            # daily loss limit
            eq = ledger.equity_now()
            start = ledger.getf("session_start_equity")
            if start > 0 and (start - eq) / start * 100 >= icfg.daily_loss_limit_pct:
                res.notes.append("daily loss limit hit; no new entries")
                ledger.set("last_intraday_ts", ts_key)
                continue

            # ---- new entries ----
            if (
                ledger.positions()
                or trades_today >= icfg.max_trades_per_day
                or t < time(9, 50)
                or t > icfg.entry_cutoff
                or t >= icfg.flat_by
                or not or_ready
            ):
                ledger.set("last_intraday_ts", ts_key)
                for p in ledger.positions():
                    df = store.load(gsym(p["symbol"]), day=day, end_ts=ts_key)
                    if not df.empty and ts in df.index:
                        ledger.mark(p["symbol"], float(df.loc[ts]["close"]))
                continue

            candidates: list = []
            for c in universe:
                df = store.load(gsym(c.symbol), day=day, end_ts=ts_key)
                if df.empty or ts not in df.index:
                    continue
                or_rng = opening_range(df, icfg.or_end)
                out = evaluate_orb(
                    c.symbol,
                    c.sector,
                    df,
                    index_upto,
                    or_rng,
                    min_gross_rr=icfg.min_gross_rr,
                    max_extension=icfg.max_breakout_extension,
                )
                if hasattr(out, "reason"):
                    res.rejections[c.symbol] = out.reason
                else:
                    candidates.append(out)

            if not candidates:
                ledger.set("last_intraday_ts", ts_key)
                continue

            best = max(candidates, key=lambda s: s.score)
            entry = best.entry
            stop = best.stop
            qty = _qty(eq, entry, stop, icfg)
            if qty < 1:
                res.notes.append(f"{best.symbol}: qty 0 (risk sizing)")
                ledger.set("last_intraday_ts", ts_key)
                continue

            slip = settings.risk.slippage_bps / 10_000
            fill = entry * (1 + slip)
            cost = order_charges(fill * qty, "BUY", settings.costs)
            if ledger.getf("cash") < fill * qty + cost:
                res.notes.append(f"{best.symbol}: insufficient cash")
                ledger.set("last_intraday_ts", ts_key)
                continue

            ledger.set("cash", ledger.getf("cash") - fill * qty - cost)
            o = {
                "symbol": best.symbol,
                "sector": best.sector,
                "qty": qty,
                "stop": best.stop,
                "target": best.target,
                "strategy": ORB_NAME,
                "regime": "INTRADAY",
            }
            ledger.open_position(day, o, fill, cost)
            ledger.mark(best.symbol, fill)
            trades_today += 1
            cap_used = fill * qty
            gross_rr = best.gross_rr
            pick = {
                "symbol": best.symbol,
                "sector": best.sector,
                "qty": qty,
                "entry": round(fill, 2),
                "stop": stop,
                "target": best.target,
                "score": best.score,
                "evidence": list(best.evidence),
                "invalidation": best.invalidation,
                "capital_used": round(cap_used, 2),
                "capital_pct_of_equity": round(100 * cap_used / eq, 2) if eq else 0.0,
                "gross_rr": round(gross_rr, 2),
            }
            res.new_entries.append(pick)
            res.fills.append(
                f"{best.symbol} long {qty} @ {fill:.2f} (Rs{cap_used:,.0f} = {pick['capital_pct_of_equity']:.1f}% of equity)"
            )
            ledger.event(day, "FILL", best.symbol, json.dumps(pick))
            ledger.set("last_intraday_ts", ts_key)

        # mark end of session
        last_ts = times[-1].strftime("%Y-%m-%d %H:%M:%S")
        for p in ledger.positions():
            df = store.load(gsym(p["symbol"]), day=day, end_ts=last_ts)
            if not df.empty:
                ledger.mark(p["symbol"], float(df["close"].iloc[-1]))
        bench = float(index_full["close"].iloc[-1]) if not index_full.empty else None
        ledger.record_equity(day, bench)
        peak = max(ledger.getf("peak_equity"), ledger.equity_now())
        ledger.set("peak_equity", peak)
        ledger.set("prev_equity", ledger.equity_now())
        ledger.set("last_processed", day.isoformat())
        ledger.con.commit()
    except Exception:
        ledger.con.rollback()
        raise

    res.equity = ledger.equity_now()
    res.cash = ledger.getf("cash")
    res.benchmark = bench if not index_full.empty else None
    res.session_pnl = res.equity - res.session_start_equity
    if res.session_start_equity > 0:
        res.session_pnl_pct = res.session_pnl / res.session_start_equity
    return res
