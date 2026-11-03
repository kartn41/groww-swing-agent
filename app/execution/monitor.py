"""Live position monitor (LIVE mode only). One cycle:

  1. Read broker holdings. If that fails, change NOTHING and alert (never act on unknown state).
  2. For every position the agent opened:
       - entry order filled more since last cycle  -> grow quantity, resize the GTT
       - shares gone (stop hit / sold manually)    -> cancel the GTT if still active, close the record
       - fewer shares than expected                -> shrink the GTT to what is actually held, alert
       - more shares than the agent bought         -> alert; protect only the agent's quantity
       - GTT missing / cancelled / expired / failed -> re-arm a new GTT, alert
       - GTT quantity or trigger out of date        -> modify it
       - GTT triggered                              -> exit in progress, leave it alone
  3. GTTs the agent created that no longer belong to an open position -> cancel (stale GTTs could
     sell shares bought later). GTTs you created yourself in the Groww app are never touched,
     because the agent only manages GTT ids recorded in its own registry.
"""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.broker.base import Broker, BrokerUnavailable, STOP_GONE, STOP_IN_PROGRESS, STOP_LIVE
from app.execution.executor import make_reference_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, qty INTEGER, stop REAL, stop_id TEXT,
  strategy TEXT, entry_ref TEXT, opened_on TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS stops(stop_id TEXT PRIMARY KEY, symbol TEXT, created TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, symbol TEXT, detail TEXT);
"""


class LiveRegistry:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    def register_entry(self, symbol: str, qty: int, stop: float, stop_id: Optional[str], strategy: str,
                       entry_ref: str, on: date) -> None:
        self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?, 'OPEN')",
                         (symbol, qty, stop, stop_id, strategy, entry_ref, on.isoformat()))
        if stop_id:
            self.add_stop(stop_id, symbol)
        self.con.commit()

    def add_stop(self, stop_id: str, symbol: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO stops VALUES (?,?,?, 'ACTIVE')",
                         (stop_id, symbol, datetime.now(timezone.utc).isoformat()))

    def open_positions(self):
        return self.con.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY symbol").fetchall()

    def update(self, symbol: str, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE positions SET {sets} WHERE symbol=?", (*cols.values(), symbol))

    def event(self, kind: str, symbol: str, detail: str) -> None:
        self.con.execute("INSERT INTO events(ts,kind,symbol,detail) VALUES (?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), kind, symbol, detail))


@dataclass
class CycleResult:
    actions: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    aborted: bool = False


class Monitor:
    def __init__(self, broker: Broker, registry: LiveRegistry):
        self.b, self.reg = broker, registry

    def cycle(self, today: date) -> CycleResult:
        res = CycleResult()
        try:
            holdings = self.b.positions()
        except BrokerUnavailable as e:
            res.aborted = True
            res.alerts.append(f"Broker positions unavailable ({e}); monitor made no changes this cycle.")
            return res
        try:
            for p in self.reg.open_positions():
                self._one(p, holdings, today, res)
            self._orphans(res)
        finally:
            self.reg.con.commit()
        return res

    # ------------------------------------------------------------------
    def _one(self, p, holdings: dict, today: date, res: CycleResult) -> None:
        sym, qty, held = p["symbol"], int(p["qty"]), int(holdings.get(p["symbol"], 0))

        if p["entry_ref"]:                                     # entry filled further since last cycle?
            try:
                st = self.b.get_order_by_reference(p["entry_ref"])
            except BrokerUnavailable:
                st = None
                res.alerts.append(f"{sym}: entry order status unavailable; quantity not re-checked.")
            if st is not None and st.filled_qty > qty and held >= st.filled_qty:
                res.actions.append(f"{sym}: entry fill grew {qty} -> {st.filled_qty}")
                qty = st.filled_qty
                self.reg.update(sym, qty=qty)

        try:
            stop = self.b.get_stop(p["stop_id"]) if p["stop_id"] else None
        except BrokerUnavailable as e:
            res.alerts.append(f"{sym}: GTT status unavailable ({e}); no change made.")
            return

        if held == 0:
            if stop is not None and stop.status in STOP_LIVE:
                self._cancel(stop.stop_id, sym, res, "position no longer held")
            self.reg.update(sym, status="CLOSED")
            self.reg.event("CLOSED", sym, f"holdings 0 (stop status {stop.status if stop else 'unknown'})")
            res.alerts.append(f"{sym}: position closed (stop executed or sold outside the agent).")
            return

        target_qty = qty
        if held < qty:
            res.alerts.append(f"{sym}: holding {held} but expected {qty} (partial stop fill or manual sale); "
                              f"stop resized to {held}.")
            target_qty = held
            self.reg.update(sym, qty=held)
        elif held > qty:
            res.alerts.append(f"{sym}: holding {held} but the agent bought {qty} (manual buy?). RECONCILE: "
                              f"only the agent's {qty} are protected.")

        if stop is None or stop.status in STOP_GONE:
            why = "missing" if stop is None else stop.status
            n = self.reg.con.execute("SELECT COUNT(*) FROM stops WHERE symbol=?", (sym,)).fetchone()[0]
            ref = make_reference_id(sym, f"{p['strategy']}-rearm{n}", today, prefix="SL")
            try:
                new_id = self.b.place_stop_protection(sym, target_qty, float(p["stop"]), ref)
            except Exception as e:
                res.alerts.append(f"CRITICAL {sym}: {target_qty} shares UNPROTECTED; re-arming GTT failed "
                                  f"({type(e).__name__}). Place a stop manually in the Groww app.")
                return
            self.reg.add_stop(new_id, sym)
            self.reg.update(sym, stop_id=new_id)
            res.alerts.append(f"{sym}: stop was {why}; re-armed GTT {new_id} for {target_qty} @ {p['stop']:.2f}.")
            return
        if stop.status in STOP_IN_PROGRESS:
            res.actions.append(f"{sym}: GTT triggered, exit in progress; no change.")
            return
        if stop.status in STOP_LIVE and (stop.qty != target_qty or abs(stop.trigger - float(p["stop"])) > 0.005):
            try:
                self.b.modify_stop(stop.stop_id, sym, target_qty, float(p["stop"]))
                res.actions.append(f"{sym}: GTT updated to {target_qty} @ {p['stop']:.2f} "
                                   f"(was {stop.qty} @ {stop.trigger:.2f}).")
            except Exception as e:
                res.alerts.append(f"{sym}: GTT modify failed ({type(e).__name__}); stop may not match holdings.")
        elif stop.status not in STOP_LIVE:
            res.alerts.append(f"{sym}: unrecognised GTT status {stop.status}; review in the Groww app.")

    def _orphans(self, res: CycleResult) -> None:
        current = {p["stop_id"] for p in self.reg.open_positions() if p["stop_id"]}
        for row in self.reg.con.execute("SELECT * FROM stops WHERE status='ACTIVE'").fetchall():
            if row["stop_id"] in current:
                continue
            try:
                st = self.b.get_stop(row["stop_id"])
            except BrokerUnavailable:
                res.alerts.append(f"{row['symbol']}: could not check old GTT {row['stop_id']}; will retry.")
                continue
            if st is not None and st.status in STOP_LIVE:
                self._cancel(row["stop_id"], row["symbol"], res, "stale GTT from a closed or replaced position")
            else:
                self.reg.con.execute("UPDATE stops SET status=? WHERE stop_id=?",
                                     (st.status if st else "UNKNOWN", row["stop_id"]))

    def _cancel(self, stop_id: str, sym: str, res: CycleResult, why: str) -> None:
        try:
            self.b.cancel_stop(stop_id)
            self.reg.con.execute("UPDATE stops SET status='CANCELLED' WHERE stop_id=?", (stop_id,))
            res.actions.append(f"{sym}: cancelled GTT {stop_id} ({why}).")
        except Exception as e:
            res.alerts.append(f"{sym}: could not cancel GTT {stop_id} ({type(e).__name__}); cancel it in the "
                              f"Groww app, or it may sell shares bought later.")
