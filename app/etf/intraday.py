"""Market-hours cycle (run every ~15 minutes): protect holdings, then execute the planned tranches.

Holdings (any time between protect_start and protect_end):
  - price <= stop / trailing stop                      -> sell
  - sudden fall from the previous close or today's high -> sell (threshold per ETF: % or ATR, whichever larger)
  - exit flagged at the last close (RSI/MACD/trend/time) -> sell inside the trading window
  - price >= target                                    -> sell
  Units bought today are never sold today (that would be an intraday trade); they are sold next session.
Planned buys (only inside 09:45-15:00): ask <= limit, price above stop, spread acceptable, 1-hour candles confirm,
15-minute candles not still falling, funds available. Limit orders only.

PAPER fills against the live quote (buy at the ask, sell at the bid). LIVE places Groww limit orders by
deterministic reference id, re-reads their status every cycle and books only what the broker reports filled."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

import pandas as pd

from app.analysis.indicators import ema, rsi
from app.broker.base import (Broker, BrokerTimeout, BrokerUnavailable, DEAD_STATUSES, FILLED_STATUSES,
                             OrderRequest, PENDING_STATUSES)
from app.etf.config import EtfConfig
from app.etf.engine import apply_buy_fill, risk_block, sell_ref, start_cooldown
from app.etf.ledger import EtfLedger
from app.etf.market import MarketDataError, Quote
from app.etf.strategy import sudden_drop_level
from app.risk.costs import order_charges

log = logging.getLogger(__name__)
PROTECT_START, PROTECT_END = time(9, 30), time(15, 25)
PROTECTIVE = ("STOP", "TRAIL_STOP", "SUDDEN_DROP", "SUDDEN_DROP_FROM_HIGH")


@dataclass
class CycleResult:
    at: datetime
    skipped: Optional[str] = None
    quotes: dict = field(default_factory=dict)
    fills: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    waiting: dict = field(default_factory=dict)
    sold: set = field(default_factory=set)


# ---------------------------------------------------------------- confirmations
def hourly_confirms(df: Optional[pd.DataFrame]) -> tuple[bool, str]:
    if df is None or len(df) < 30:
        return False, "not enough completed 1-hour candles"
    c = df["close"]
    e9, r = ema(c, 9), rsi(c, 14)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    if last >= float(e9.iloc[-1]) and float(r.iloc[-1]) > float(r.iloc[-2]):
        return True, f"1h close {last:.2f} above 1h EMA9 {float(e9.iloc[-1]):.2f}, 1h RSI rising {float(r.iloc[-1]):.0f}"
    if last > prev > float(c.iloc[-3]):
        return True, "two rising 1-hour closes"
    return False, f"1h trend not confirming (close {last:.2f}, EMA9 {float(e9.iloc[-1]):.2f}, RSI {float(r.iloc[-1]):.0f})"


def m15_timing(df: Optional[pd.DataFrame]) -> tuple[bool, str]:
    if df is None or len(df) < 4:
        return False, "not enough completed 15-minute candles"
    last = df.iloc[-1]
    prior_low = float(df["low"].iloc[-4:-1].min())
    if float(last["low"]) < prior_low and float(last["close"]) < float(last["open"]):
        return False, f"15m still making lower lows ({float(last['low']):.2f} < {prior_low:.2f})"
    if float(last["close"]) >= float(last["open"]) or float(last["close"]) > float(df["close"].iloc[-2]):
        return True, f"15m candle closed firm at {float(last['close']):.2f}"
    return False, "last 15m candle weak; waiting for the next one"


# ---------------------------------------------------------------- executors
class PaperExecution:
    mode = "PAPER"

    def __init__(self, cfg: EtfConfig):
        self.cfg = cfg

    def cash(self, ledger: EtfLedger) -> float:
        return ledger.available_cash(self.cfg.same_day_proceeds_pct)

    def sync(self, ledger: EtfLedger, d: date, res: CycleResult) -> None:
        pass

    def buy(self, ledger: EtfLedger, o, q: Quote, d: date, res: CycleResult) -> None:
        prof = self.cfg.profile(o["symbol"])
        fill = round(q.ask * (1 + prof.slippage_bps / 2 / 1e4), 4)
        cost = order_charges(fill * o["qty"], "BUY", self.cfg.costs)
        if fill * o["qty"] + cost > self.cash(ledger):
            res.waiting[o["symbol"]] = "not enough settled paper cash"
            return
        apply_buy_fill(d, ledger, o, o["qty"], fill, cost)
        res.fills.append(f"BUY T{o['tranche']} {o['kind']} {o['qty']} {o['symbol']} @ {fill:.2f} (paper, ask {q.ask:.2f})")

    def sell(self, ledger: EtfLedger, sym: str, qty: int, q: Quote, reason: str, d: date, res: CycleResult) -> None:
        prof = self.cfg.profile(sym)
        px = (q.bid or q.ltp) * (1 - prof.slippage_bps / 2 / 1e4)
        cost = order_charges(px * qty, "SELL", self.cfg.costs)
        net = ledger.sell(d, sym, qty, px, cost, reason)
        res.exits.append(f"SELL {qty} {sym} @ {px:.2f} [{reason}] net Rs{net:+,.0f} (paper)")

    def end_window(self, ledger: EtfLedger, d: date, res: CycleResult) -> None:
        pass


class LiveExecution:
    """Groww limit orders. The ledger only changes when the broker reports a fill."""
    mode = "LIVE"

    def __init__(self, broker: Broker, cfg: EtfConfig):
        self.b, self.cfg = broker, cfg

    def cash(self, ledger: EtfLedger) -> float:
        return self.b.available_cash()

    def _place(self, ledger: EtfLedger, ref: str, sym: str, side: str, qty: int, price: float, res: CycleResult) -> bool:
        try:
            existing = self.b.get_order_by_reference(ref)
        except BrokerUnavailable as e:
            res.alerts.append(f"{sym}: cannot check for an existing order ({e}); not placing")
            return False
        if existing is not None:
            ledger.update_order(ref, status="OPEN", broker_id=existing.broker_order_id)
            return True
        try:
            ack = self.b.place_order(OrderRequest(sym, qty, side, "LIMIT", round(price, 2), None, ref))
            ledger.update_order(ref, status="OPEN", broker_id=ack.broker_order_id)
        except BrokerTimeout as e:
            ledger.update_order(ref, status="OPEN", reason=f"outcome unknown ({e}); reconciling by reference")
            res.alerts.append(f"{sym}: {side} order outcome unknown; will reconcile by reference {ref}, no retry")
        return True

    def buy(self, ledger: EtfLedger, o, q: Quote, d: date, res: CycleResult) -> None:
        price = min(q.ask, o["limit_price"])
        try:
            cash = self.b.available_cash()
        except BrokerUnavailable as e:
            res.alerts.append(f"{o['symbol']}: funds unknown ({e}); buy skipped")
            return
        need = price * o["qty"] + order_charges(price * o["qty"], "BUY", self.cfg.costs)
        if need > cash:
            res.waiting[o["symbol"]] = f"Groww available funds Rs{cash:,.0f} < Rs{need:,.0f} needed"
            return
        if self._place(ledger, o["ref"], o["symbol"], "BUY", o["qty"], price, res):
            res.fills.append(f"BUY LIMIT {o['qty']} {o['symbol']} @ {price:.2f} sent (T{o['tranche']})")
        self.sync(ledger, d, res)

    def sell(self, ledger: EtfLedger, sym: str, qty: int, q: Quote, reason: str, d: date, res: CycleResult) -> None:
        try:
            held = self.b.positions().get(sym, 0)
        except BrokerUnavailable as e:
            res.alerts.append(f"CRITICAL {sym}: holdings unknown ({e}); {reason} sale NOT sent. Check the Groww app.")
            return
        if held < qty:
            res.alerts.append(f"{sym}: Groww shows {held} units, ledger expects {qty}; selling {held}. RECONCILE.")
            qty = held
        if qty <= 0:
            return
        n = ledger.con.execute("SELECT COUNT(*) FROM orders WHERE symbol=? AND side='SELL' AND d=?",
                               (sym, d.isoformat())).fetchone()[0]
        ref = sell_ref(sym, reason, d, n)
        price = q.bid or q.ltp
        ledger.add_order(ref, d, sym, "SELL", qty, price, reason=reason, status="OPEN")
        if self._place(ledger, ref, sym, "SELL", qty, price, res):
            res.exits.append(f"SELL LIMIT {qty} {sym} @ {price:.2f} sent [{reason}]")
        self.sync(ledger, d, res)

    def sync(self, ledger: EtfLedger, d: date, res: CycleResult) -> None:
        for o in ledger.open_orders():
            if o["status"] == "PLANNED":
                continue
            try:
                st = self.b.get_order_by_reference(o["ref"])
            except BrokerUnavailable as e:
                res.alerts.append(f"{o['symbol']}: order {o['ref']} status unavailable ({e})")
                continue
            if st is None:
                ledger.update_order(o["ref"], status="REJECTED", reason="not found at broker")
                continue
            delta = st.filled_qty - (o["filled_qty"] or 0)
            if delta > 0:
                prev_val = (o["filled_qty"] or 0) * (o["avg_fill"] or 0.0)
                px = (st.filled_qty * st.avg_price - prev_val) / delta if st.avg_price else o["limit_price"]
                done = st.status in FILLED_STATUSES and st.filled_qty >= o["qty"]
                if o["side"] == "BUY":
                    cost = order_charges(px * delta, "BUY", self.cfg.costs)
                    apply_buy_fill(d, ledger, o, delta, px, cost, total_filled=st.filled_qty, done=done)
                    res.fills.append(f"FILLED BUY {delta} {o['symbol']} @ {px:.2f} ({st.filled_qty}/{o['qty']})")
                else:
                    cost = order_charges(px * delta, "SELL", self.cfg.costs)
                    net = ledger.sell(d, o["symbol"], delta, px, cost, o["reason"] or "SELL")
                    ledger.update_order(o["ref"], filled_qty=st.filled_qty, avg_fill=st.avg_price,
                                        status="FILLED" if done else "PARTIAL")
                    res.exits.append(f"FILLED SELL {delta} {o['symbol']} @ {px:.2f} net Rs{net:+,.0f}")
            if st.status in DEAD_STATUSES:
                ledger.update_order(o["ref"], status="CANCELLED" if st.status == "CANCELLED" else "REJECTED",
                                    reason=(o["reason"] or "") + f" | broker {st.status}")
                if st.filled_qty < o["qty"]:
                    res.alerts.append(f"{o['symbol']}: {o['side']} order {st.status} with {st.filled_qty}/{o['qty']} filled")
            elif st.status not in FILLED_STATUSES and st.status not in PENDING_STATUSES:
                res.alerts.append(f"{o['symbol']}: unrecognised order status {st.status}; review in the Groww app")

    def cancel_stale_sells(self, ledger: EtfLedger, now: datetime, res: CycleResult, max_age_s: float = 300) -> None:
        """A sell limit left behind by a falling price is cancelled; the next cycle re-sends it at the new bid."""
        for o in ledger.open_orders(side="SELL"):
            age = (now - datetime.fromisoformat(o["updated"]).astimezone(now.tzinfo)).total_seconds()
            if o["broker_id"] and age >= max_age_s:
                try:
                    self.b.cancel_order(o["broker_id"])
                    res.alerts.append(f"{o['symbol']}: unfilled sell at {o['limit_price']:.2f} cancelled; re-pricing")
                except Exception as e:
                    res.alerts.append(f"{o['symbol']}: could not cancel stale sell ({type(e).__name__})")

    def end_window(self, ledger: EtfLedger, d: date, res: CycleResult) -> None:
        """Unfilled BUY limits are cancelled after the window closes (plans are valid for one session)."""
        for o in ledger.open_orders(side="BUY"):
            if o["status"] in ("OPEN", "PARTIAL") and o["broker_id"]:
                try:
                    self.b.cancel_order(o["broker_id"])
                    res.alerts.append(f"{o['symbol']}: unfilled buy cancelled at window end")
                except Exception as e:
                    res.alerts.append(f"{o['symbol']}: could not cancel buy {o['broker_id']} ({type(e).__name__})")
        self.sync(ledger, d, res)


# ---------------------------------------------------------------- the cycle
def run_cycle(now: datetime, ledger: EtfLedger, market, execu, cfg: EtfConfig, prev_closes: dict,
              kill_switch: bool = False, protect_start: time = PROTECT_START, protect_end: time = PROTECT_END) -> CycleResult:
    res = CycleResult(now)
    d, t = now.date(), now.time()
    if now.weekday() >= 5:
        res.skipped = "weekend"
        return res
    if not (protect_start <= t <= protect_end):
        res.skipped = f"outside monitoring hours {protect_start:%H:%M}-{protect_end:%H:%M}"
        return res
    ledger.settle(d)
    execu.sync(ledger, d, res)
    if hasattr(execu, "cancel_stale_sells"):
        execu.cancel_stale_sells(ledger, now, res)
        execu.sync(ledger, d, res)
    syms = sorted({p["symbol"] for p in ledger.positions()} | {o["symbol"] for o in ledger.open_orders()}
                  | set(prev_closes))
    for sym in syms:
        try:
            res.quotes[sym] = market.quote(sym)
        except MarketDataError as e:
            res.alerts.append(f"{sym}: no live price ({e})" + ("; holding NOT protected this cycle"
                                                               if ledger.position(sym) else ""))
    for sym, q in res.quotes.items():
        key = f"dayhigh_{sym}_{d.isoformat()}"
        hi = max(ledger.getf(key, 0.0), q.day_high or 0.0, q.ltp)
        ledger.set(key, hi)

    in_window = cfg.window_start <= t <= cfg.window_end
    _protect(d, ledger, execu, cfg, prev_closes, res, in_window)

    if t > cfg.window_end:
        execu.end_window(ledger, d, res)
    elif in_window:
        _entries(d, ledger, market, execu, cfg, res, kill_switch)
    ledger.set("last_cycle", now.isoformat())
    ledger.commit()
    return res


def _protect(d, ledger: EtfLedger, execu, cfg: EtfConfig, prev_closes: dict, res: CycleResult, in_window: bool) -> None:
    for p in ledger.positions():
        sym = p["symbol"]
        q = res.quotes.get(sym)
        if q is None:
            continue
        if ledger.open_orders(symbol=sym, side="SELL"):
            continue                                     # a sale is already working
        qty = ledger.sellable_qty(sym, d)
        if qty <= 0:
            continue
        prof = cfg.profile(sym)
        prev = prev_closes.get(sym) or p["last_close"]
        day_high = ledger.getf(f"dayhigh_{sym}_{d.isoformat()}", q.ltp)
        lvl_prev, lvl_high = sudden_drop_level(prev, day_high, p["atr_pct"] or 0.01, prof)
        reason = None
        if q.ltp <= p["stop"]:
            reason = "TRAIL_STOP" if p["stop"] >= p["avg"] else "STOP"
        elif q.ltp <= lvl_prev:
            reason = "SUDDEN_DROP"
        elif q.ltp <= lvl_high:
            reason = "SUDDEN_DROP_FROM_HIGH"
        elif in_window and p["exit_pending"]:
            reason = p["exit_pending"]
        elif q.ltp >= p["target"]:
            reason = "TARGET_EXTENDED" if p["extended"] else "TARGET"
        if not reason:
            continue
        sp = q.spread_bps
        if reason not in PROTECTIVE and (sp is None or sp > prof.max_spread_bps):
            res.waiting[sym] = f"{reason}: spread {'unknown' if sp is None else f'{sp:.0f} bps'} too wide; retry next cycle"
            continue
        if reason.startswith("SUDDEN"):
            res.alerts.append(f"SUDDEN FALL {sym}: {q.ltp:.2f} vs previous close {prev:.2f} ({q.ltp / prev - 1:+.2%}), "
                              f"day high {day_high:.2f}; selling {qty} units")
        execu.sell(ledger, sym, qty, q, reason, d, res)
        res.sold.add(sym)
        if reason in PROTECTIVE:
            start_cooldown(ledger, sym, d, cfg.cooldown_days)


def _entries(d, ledger: EtfLedger, market, execu, cfg: EtfConfig, res: CycleResult, kill_switch: bool) -> None:
    plans = [o for o in ledger.open_orders(side="BUY") if o["status"] == "PLANNED" and o["d"] < d.isoformat()]
    if not plans:
        return
    if kill_switch:
        res.waiting.update({o["symbol"]: "kill switch active" for o in plans})
        return
    equity = ledger.getf("cash") + ledger.getf("unsettled") + sum(
        p["qty"] * (res.quotes[p["symbol"]].ltp if p["symbol"] in res.quotes else p["last_close"])
        for p in ledger.positions())
    block = risk_block(ledger, cfg, equity)
    if block:
        res.waiting.update({o["symbol"]: block for o in plans})
        return
    for o in plans:
        sym = o["symbol"]
        q = res.quotes.get(sym)
        if q is None or q.ask is None:
            res.waiting[sym] = "no bid/ask quote; not buying blind"
            continue
        if sym in res.sold:
            res.waiting[sym] = "sold this cycle"
            continue
        if q.ltp <= o["stop"]:
            ledger.update_order(o["ref"], status="CANCELLED", reason="price at/below planned stop: setup invalidated")
            res.waiting[sym] = f"plan cancelled: price {q.ltp:.2f} <= stop {o['stop']:.2f}"
            continue
        if q.ask > o["limit_price"]:
            res.waiting[sym] = f"ask {q.ask:.2f} above limit {o['limit_price']:.2f}; not chasing"
            continue
        sp, prof = q.spread_bps, cfg.profile(sym)
        if sp is None or sp > prof.max_spread_bps:
            res.waiting[sym] = f"spread {'unknown' if sp is None else f'{sp:.0f} bps'} > {prof.max_spread_bps:.0f} bps"
            continue
        try:
            h1 = market.candles(sym, "1h", 15)
            m15 = market.candles(sym, "15m", 3)
        except MarketDataError as e:
            res.waiting[sym] = f"intraday candles unavailable ({e}); not buying"
            continue
        ok1, why1 = hourly_confirms(h1)
        ok2, why2 = m15_timing(m15)
        if not (ok1 and ok2):
            res.waiting[sym] = f"waiting: {why1 if not ok1 else why2}"
            continue
        ledger.update_order(o["ref"], reason=" | ".join(x for x in (o["reason"], f"{why1}; {why2}") if x))
        execu.buy(ledger, ledger.order(o["ref"]), q, d, res)
