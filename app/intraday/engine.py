"""Process one session bar-by-bar on 5m data (paper MIS-style, long-only ORB).

Bars are labelled by their START time. A signal is decided on the close of bar t and filled at the
open of bar t+1, so no decision sees the bar it trades on. Inside a bar the stop is assumed to hit
before the target. The whole session commits atomically."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
import pandas as pd

from app.config import Settings
from app.data.intraday_store import IntradayStore
from app.intraday.config import IntradayConfig, load_intraday_config
from app.intraday.costs import intraday_charges
from app.intraday.orb import NAME as ORB_NAME, NO_BREAKOUT, evaluate_orb, index_ok
from app.paper.ledger import Ledger
from app.paper.simulator import Bar, exit_check
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
    data_issues: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    equity: float = 0.0
    cash: float = 0.0
    benchmark: Optional[float] = None
    session_start_equity: float = 0.0
    session_pnl: float = 0.0
    session_pnl_pct: float = 0.0
    new_entries: list[dict] = field(default_factory=list)
    index_blocked: str = ""
    skipped: bool = False


def _bar(df: pd.DataFrame, ts) -> Optional[Bar]:
    if ts not in df.index:
        return None
    r = df.loc[ts]
    return Bar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))


def size_position(equity: float, cash: float, entry: float, stop: float, cfg: IntradayConfig) -> int:
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return 0
    qty = int(equity * cfg.risk_per_trade_pct / 100 // per_share)
    qty = min(qty, int(equity * cfg.max_position_pct / 100 // entry), int(cash * 0.99 // entry))
    return max(qty, 0)


def _sell(day: date, ledger: Ledger, p, price: float, reason: str, slip: float, res: SessionResult) -> float:
    px = price * (1 - slip)
    sell_cost = intraday_charges(px * p["qty"], "SELL")
    net = ledger.close_position(day, p, px, reason, sell_cost)
    ledger.set("cash", ledger.getf("cash") + px * p["qty"] - sell_cost)
    res.exits.append(f"{p['symbol']} {reason} @ {px:.2f}, net Rs{net:+,.0f}")
    ledger.event(day, "EXIT", p["symbol"], res.exits[-1])
    return net


def _equity(ledger: Ledger, frames: dict, ts) -> float:
    for p in ledger.positions():
        b = _bar(frames[p["symbol"]], ts)
        if b is not None:
            ledger.mark(p["symbol"], b.close)
    return ledger.equity_now()


def process_session(day: date, ledger: Ledger, store: IntradayStore, universe: list[Constituent],
                    settings: Settings, icfg: Optional[IntradayConfig] = None) -> SessionResult:
    icfg = icfg or load_intraday_config()
    res = SessionResult(day)
    last = ledger.get("last_processed")
    if last and date.fromisoformat(last) >= day:
        res.skipped = True
        res.notes.append(f"{day} already processed (last {last})")
        return res

    index_df = store.load(INDEX_SYMBOL, day=day)
    if len(index_df) < icfg.min_session_bars:
        res.skipped = True
        res.notes.append(f"NIFTY has {len(index_df)} 5m bars for {day} (< {icfg.min_session_bars}): "
                         "no session or incomplete data")
        return res

    frames: dict[str, pd.DataFrame] = {}
    sectors: dict[str, str] = {}
    for c in universe:
        df = store.load(gsym(c.symbol), day=day)
        if len(df) < icfg.min_session_bars:
            res.data_issues[c.symbol] = f"{len(df)} bars"
            continue
        frames[c.symbol], sectors[c.symbol] = df, c.sector

    res.session_start_equity = ledger.equity_now()
    try:
        _run(day, ledger, frames, sectors, index_df, settings, icfg, res)
        res.benchmark = float(index_df["close"].iloc[-1])
        ledger.record_equity(day, res.benchmark)
        ledger.set("peak_equity", max(ledger.getf("peak_equity"), ledger.equity_now()))
        ledger.set("prev_equity", ledger.equity_now())
        ledger.end_day(day)
    except Exception:
        ledger.con.rollback()
        raise

    res.equity = ledger.equity_now()
    res.cash = ledger.getf("cash")
    res.session_pnl = res.equity - res.session_start_equity
    if res.session_start_equity > 0:
        res.session_pnl_pct = res.session_pnl / res.session_start_equity
    return res


def _run(day, ledger: Ledger, frames, sectors, index_df, settings: Settings, icfg: IntradayConfig,
         res: SessionResult) -> None:
    slip = icfg.slippage_bps / 10_000
    start_eq = res.session_start_equity
    pending: Optional[dict] = None
    init_risk: dict[str, float] = {}
    at_breakeven: set[str] = set()
    trades = 0
    halted = ""

    for ts in index_df.index:
        t = ts.time()

        # 1. fill the signal from the previous bar at this bar's open
        if pending is not None:
            s, qty, pending_eq = pending["setup"], pending["qty"], pending["equity"]
            pending = None
            b = _bar(frames[s.symbol], ts)
            if b is None:
                res.notes.append(f"{s.symbol}: no bar at {t:%H:%M}; entry skipped")
            elif b.open > s.entry * (1 + icfg.max_chase_pct / 100):
                res.notes.append(f"{s.symbol}: opened {b.open:.2f}, > {icfg.max_chase_pct}% above signal; not chasing")
            elif b.open <= s.stop:
                res.notes.append(f"{s.symbol}: opened {b.open:.2f} at/below stop; entry skipped")
            else:
                fill = b.open * (1 + slip)
                risk = fill - s.stop
                if risk / fill * 100 > icfg.max_risk_pct:
                    res.notes.append(f"{s.symbol}: stop {risk / fill:.2%} below the fill, too wide; entry skipped")
                else:
                    target = round(fill + icfg.target_r * risk, 2)
                    qty = min(qty, size_position(pending_eq, ledger.getf("cash"), fill, s.stop, icfg))
                    if qty >= 1:
                        _open(day, ledger, s, qty, fill, target, res)
                        init_risk[s.symbol] = risk
                        trades += 1

        # 2. manage open positions on this bar
        for p in ledger.positions():
            b = _bar(frames[p["symbol"]], ts)
            if b is None:
                continue
            if t >= icfg.flat_by:
                net = _sell(day, ledger, p, b.open, "INTRADAY_EOD", slip, res)
            else:
                reason, px = exit_check(b, p["stop"], p["target"], entry_day=False)
                if reason is None:
                    r = init_risk.get(p["symbol"], 0.0)
                    if (icfg.breakeven_at_r > 0 and r > 0 and p["symbol"] not in at_breakeven
                            and b.high >= p["entry"] + icfg.breakeven_at_r * r):
                        ledger.update_stop(p["symbol"], round(p["entry"], 2))
                        at_breakeven.add(p["symbol"])
                        res.notes.append(f"{p['symbol']}: +{icfg.breakeven_at_r:g}R reached, stop moved to entry")
                    continue
                if reason == "STOP" and p["symbol"] in at_breakeven:
                    reason = "BREAKEVEN"
                net = _sell(day, ledger, p, px, f"INTRADAY_{reason}", slip, res)
            if net < 0 and icfg.stop_after_loss and not halted:
                halted = "stopped for the day after a losing trade"

        # 3. daily loss limit on marked equity
        eq = _equity(ledger, frames, ts)
        if not halted and start_eq > 0 and (start_eq - eq) / start_eq * 100 >= icfg.daily_loss_limit_pct:
            halted = f"daily loss limit {icfg.daily_loss_limit_pct}% hit"

        # 4. new signal on this bar's close, filled next bar
        if (halted or ledger.positions() or trades >= icfg.max_trades_per_day
                or t < icfg.or_end or t > icfg.entry_end or t >= icfg.flat_by):
            continue
        idx_upto = index_df.loc[:ts]
        ok, why = index_ok(idx_upto, icfg)
        if not ok:
            res.index_blocked = why
            continue
        candidates = []
        for sym, df in frames.items():
            if ts not in df.index:
                continue
            out = evaluate_orb(sym, sectors[sym], df.loc[:ts], idx_upto, icfg)
            if hasattr(out, "reason"):
                if out.reason != NO_BREAKOUT:
                    res.rejections[sym] = f"{t:%H:%M} {out.reason}"
            else:
                candidates.append(out)
        if not candidates:
            continue
        best = max(candidates, key=lambda s: s.score)
        qty = size_position(eq, ledger.getf("cash"), best.entry, best.stop, icfg)
        if qty < 1:
            res.notes.append(f"{best.symbol}: position size 0 after risk caps")
            continue
        pending = {"setup": best, "qty": qty, "equity": eq}

    if halted:
        res.notes.append(halted)
    if res.index_blocked and not res.new_entries:
        res.notes.append(f"market filter: {res.index_blocked}")
    last_ts = index_df.index[-1]
    for p in ledger.positions():
        df = frames[p["symbol"]]
        upto = df.loc[:last_ts]
        _sell(day, ledger, p, float(upto["close"].iloc[-1]), "INTRADAY_EOD_LAST_BAR", slip, res)


def _open(day: date, ledger: Ledger, s, qty: int, fill: float, target: float, res: SessionResult) -> None:
    eq = ledger.equity_now()
    buy_cost = intraday_charges(fill * qty, "BUY")
    ledger.set("cash", ledger.getf("cash") - fill * qty - buy_cost)
    ledger.open_position(day, {"symbol": s.symbol, "sector": s.sector, "qty": qty, "stop": s.stop,
                               "target": target, "strategy": ORB_NAME, "regime": "INTRADAY"}, fill, buy_cost)
    cap_used = fill * qty
    pick = {
        "symbol": s.symbol, "sector": s.sector, "qty": qty, "entry": round(fill, 2), "stop": s.stop,
        "target": target, "score": s.score, "evidence": list(s.evidence), "invalidation": s.invalidation,
        "capital_used": round(cap_used, 2),
        "capital_pct_of_equity": round(100 * cap_used / eq, 2) if eq else 0.0,
        "gross_rr": round((target - fill) / (fill - s.stop), 2),
    }
    res.new_entries.append(pick)
    res.fills.append(f"{s.symbol} long {qty} @ {fill:.2f} (Rs{cap_used:,.0f} = {pick['capital_pct_of_equity']:.1f}% of equity)")
    ledger.event(day, "FILL", s.symbol, json.dumps(pick))
