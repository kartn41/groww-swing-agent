"""Persistent GoldBEES/SilverBEES account (SQLite). Used by paper trading, the live registry and the backtest.
Each tranche is its own row (entry price, date, reason, exit); the position row holds the weighted average.
trades/equity tables match app.backtest.stats so the same statistics apply."""
from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

from app.etf.strategy import PositionView

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, qty INTEGER, avg REAL, stop REAL, target REAL,
  extended INTEGER DEFAULT 0, tranches INTEGER, last_tranche_price REAL, last_tranche_date TEXT, opened_date TEXT,
  peak_close REAL, planned_value REAL, risk REAL, regime TEXT, last_close REAL, exit_pending TEXT,
  sessions_held INTEGER DEFAULT 0, sessions_since_tranche INTEGER DEFAULT 0, buy_cost REAL DEFAULT 0,
  atr_pct REAL DEFAULT 0.01);
CREATE TABLE IF NOT EXISTS tranches(id INTEGER PRIMARY KEY, symbol TEXT, tranche_no INTEGER, kind TEXT, qty INTEGER,
  price REAL, d TEXT, buy_cost REAL, reason TEXT, status TEXT DEFAULT 'OPEN', exit_price REAL, exit_d TEXT,
  exit_reason TEXT, net REAL);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, ref TEXT UNIQUE, d TEXT, symbol TEXT, side TEXT, qty INTEGER,
  limit_price REAL, stop REAL, target REAL, tranche INTEGER, kind TEXT, status TEXT, filled_qty INTEGER DEFAULT 0,
  avg_fill REAL DEFAULT 0, reason TEXT, plan TEXT, broker_id TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, symbol TEXT, sector TEXT, qty INTEGER, entry_date TEXT,
  entry REAL, exit_date TEXT, exit REAL, reason TEXT, gross REAL, costs REAL, net REAL, strategy TEXT, regime TEXT,
  risk REAL, tranches INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, d TEXT, kind TEXT, symbol TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(d TEXT PRIMARY KEY, cash REAL, equity REAL, benchmark REAL, invested REAL);
"""
OPEN_ORDER = ("PLANNED", "OPEN", "PARTIAL")


class EtfLedger:
    def __init__(self, path: str, start_capital: float) -> None:
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        if self.get("cash") is None:
            for k in ("start_capital", "cash", "peak_equity", "prev_equity", "day_start_equity"):
                self.set(k, start_capital)
            self.set("unsettled", 0.0)
        self.con.commit()

    # ---- meta ----
    def get(self, key: str) -> Optional[str]:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def getf(self, key: str, default: float = 0.0) -> float:
        v = self.get(key)
        return float(v) if v is not None else default

    def set(self, key: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def event(self, d: date, kind: str, symbol: str = "", detail: str = "") -> None:
        self.con.execute("INSERT INTO events(ts,d,kind,symbol,detail) VALUES (?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), d.isoformat(), kind, symbol, detail))

    def commit(self) -> None:
        self.con.commit()

    # ---- cash and settlement ----
    def settle(self, d: date) -> None:
        """Proceeds of sales made before `d` become usable on `d`."""
        rel = self.get("unsettled_since")
        if rel and date.fromisoformat(rel) < d and self.getf("unsettled") > 0:
            self.set("cash", self.getf("cash") + self.getf("unsettled"))
            self.set("unsettled", 0.0)

    def available_cash(self, same_day_pct: float = 0.0) -> float:
        return self.getf("cash") + self.getf("unsettled") * same_day_pct / 100

    # ---- positions ----
    def positions(self):
        return self.con.execute("SELECT * FROM positions ORDER BY symbol").fetchall()

    def position(self, symbol: str):
        return self.con.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()

    def view(self, symbol: str) -> Optional[PositionView]:
        p = self.position(symbol)
        if p is None:
            return None
        return PositionView(symbol, p["qty"], p["avg"], p["stop"], p["target"], p["tranches"],
                            p["last_tranche_price"], p["sessions_since_tranche"], p["sessions_held"],
                            p["peak_close"], p["planned_value"], bool(p["extended"]))

    def sellable_qty(self, symbol: str, today: date) -> int:
        """Units bought before today. Selling today's purchase would be an intraday trade (not allowed)."""
        r = self.con.execute("SELECT COALESCE(SUM(qty),0) FROM tranches WHERE symbol=? AND status='OPEN' AND d<?",
                             (symbol, today.isoformat())).fetchone()
        return int(r[0])

    def tranches(self, symbol: Optional[str] = None, status: Optional[str] = "OPEN"):
        q, a = "SELECT * FROM tranches WHERE 1=1", []
        if symbol:
            q, a = q + " AND symbol=?", a + [symbol]
        if status:
            q, a = q + " AND status=?", a + [status]
        return self.con.execute(q + " ORDER BY id", a).fetchall()

    def buy_fill(self, d: date, symbol: str, qty: int, price: float, cost: float, *, tranche: int, kind: str,
                 stop: float, target: float, planned_value: float, regime: str, reason: str,
                 same_order: bool = False) -> None:
        """same_order=True: a further partial fill of the order that created the latest tranche (merged into it)."""
        self.set("cash", self.getf("cash") - qty * price - cost)
        p = self.position(symbol)
        last = self.con.execute("SELECT * FROM tranches WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                                (symbol,)).fetchone()
        if same_order and p is not None and last is not None and last["tranche_no"] == tranche:
            tq = last["qty"] + qty
            self.con.execute("UPDATE tranches SET qty=?, price=?, buy_cost=buy_cost+? WHERE id=?",
                             (tq, (last["qty"] * last["price"] + qty * price) / tq, cost, last["id"]))
            tot = p["qty"] + qty
            avg = (p["qty"] * p["avg"] + qty * price) / tot
            self.con.execute("UPDATE positions SET qty=?, avg=?, target=?, risk=?, buy_cost=buy_cost+? WHERE symbol=?",
                             (tot, avg, target, (avg - p["stop"]) * tot, cost, symbol))
            self.event(d, "BUY", symbol, f"T{tranche} +{qty} @ {price:.2f} (further partial fill)")
            return
        self.con.execute("INSERT INTO tranches(symbol,tranche_no,kind,qty,price,d,buy_cost,reason) VALUES (?,?,?,?,?,?,?,?)",
                         (symbol, tranche, kind, qty, price, d.isoformat(), cost, reason))
        if p is None:
            self.con.execute(
                "INSERT INTO positions(symbol,qty,avg,stop,target,tranches,last_tranche_price,last_tranche_date,"
                "opened_date,peak_close,planned_value,risk,regime,last_close,buy_cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, qty, price, stop, target, 1, price, d.isoformat(), d.isoformat(), price, planned_value,
                 (price - stop) * qty + cost, regime, price, cost))
        else:
            tot = p["qty"] + qty
            avg = (p["qty"] * p["avg"] + qty * price) / tot
            self.con.execute(
                "UPDATE positions SET qty=?, avg=?, stop=?, target=?, tranches=?, last_tranche_price=?, "
                "last_tranche_date=?, sessions_since_tranche=0, risk=?, buy_cost=buy_cost+? WHERE symbol=?",
                (tot, avg, min(stop, p["stop"]), target, p["tranches"] + 1, price, d.isoformat(),
                 max(p["risk"] or 0, (avg - min(stop, p["stop"])) * tot), cost, symbol))
        self.event(d, "BUY", symbol, f"T{tranche} {kind} {qty} @ {price:.2f}: {reason}")

    def sell(self, d: date, symbol: str, qty: int, price: float, cost: float, reason: str) -> float:
        """Sell `qty` units FIFO (only units bought before d). Returns net P&L of the sold units."""
        p = self.position(symbol)
        left, gross, buy_costs, sold = qty, 0.0, 0.0, 0
        for t in self.tranches(symbol):
            if left <= 0 or t["d"] >= d.isoformat():
                continue
            n = min(left, t["qty"])
            share = n / t["qty"]
            g = (price - t["price"]) * n
            bc = t["buy_cost"] * share
            tnet = g - bc - cost * n / qty
            if n == t["qty"]:
                self.con.execute("UPDATE tranches SET status='SOLD', exit_price=?, exit_d=?, exit_reason=?, net=? "
                                 "WHERE id=?", (price, d.isoformat(), reason, tnet, t["id"]))
            else:
                self.con.execute("UPDATE tranches SET qty=qty-?, buy_cost=buy_cost-? WHERE id=?", (n, bc, t["id"]))
                self.con.execute("INSERT INTO tranches(symbol,tranche_no,kind,qty,price,d,buy_cost,reason,status,"
                                 "exit_price,exit_d,exit_reason,net) VALUES (?,?,?,?,?,?,?,?,'SOLD',?,?,?,?)",
                                 (symbol, t["tranche_no"], t["kind"], n, t["price"], t["d"], bc, t["reason"],
                                  price, d.isoformat(), reason, tnet))
            gross, buy_costs, left, sold = gross + g, buy_costs + bc, left - n, sold + n
        if sold == 0:
            return 0.0
        net = gross - buy_costs - cost
        entry = price - gross / sold
        self.con.execute(
            "INSERT INTO trades(symbol,sector,qty,entry_date,entry,exit_date,exit,reason,gross,costs,net,strategy,regime,"
            "risk,tranches) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, "ETF", sold, p["opened_date"], entry, d.isoformat(), price, reason, gross, buy_costs + cost, net,
             "etf_dip", p["regime"], (p["risk"] or 0) * sold / p["qty"], p["tranches"]))
        self.set("unsettled", self.getf("unsettled") + sold * price - cost)
        self.set("unsettled_since", d.isoformat())
        remaining = p["qty"] - sold
        if remaining <= 0:
            self.con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        else:
            self.con.execute("UPDATE positions SET qty=?, buy_cost=buy_cost-?, risk=risk*? WHERE symbol=?",
                             (remaining, buy_costs, remaining / p["qty"], symbol))
        self.event(d, "SELL", symbol, f"{sold} @ {price:.2f} [{reason}] net Rs{net:+,.0f}")
        return net

    def update_position(self, symbol: str, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE positions SET {sets} WHERE symbol=?", (*cols.values(), symbol))

    # ---- orders ----
    def add_order(self, ref: str, d: date, symbol: str, side: str, qty: int, limit: float, *, stop: float = 0.0,
                  target: float = 0.0, tranche: int = 0, kind: str = "", reason: str = "", plan: dict | None = None,
                  status: str = "PLANNED") -> bool:
        """False if an order with this reference already exists (idempotent)."""
        try:
            self.con.execute(
                "INSERT INTO orders(ref,d,symbol,side,qty,limit_price,stop,target,tranche,kind,status,reason,plan,updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, d.isoformat(), symbol, side, qty, limit, stop, target, tranche, kind, status, reason,
                 json.dumps(plan or {}), datetime.now(timezone.utc).isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False

    def order(self, ref: str):
        return self.con.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()

    def open_orders(self, symbol: Optional[str] = None, side: Optional[str] = None):
        q = f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(OPEN_ORDER))})"
        a: list = list(OPEN_ORDER)
        if symbol:
            q, a = q + " AND symbol=?", a + [symbol]
        if side:
            q, a = q + " AND side=?", a + [side]
        return self.con.execute(q + " ORDER BY id", a).fetchall()

    def update_order(self, ref: str, **cols) -> None:
        cols["updated"] = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE orders SET {sets} WHERE ref=?", (*cols.values(), ref))

    # ---- valuation ----
    def invested(self) -> float:
        return sum(p["qty"] * p["last_close"] for p in self.positions())

    def exposure(self) -> dict:
        return {p["symbol"]: p["qty"] * p["last_close"] for p in self.positions()}

    def equity_now(self) -> float:
        return self.getf("cash") + self.getf("unsettled") + self.invested()

    def record_equity(self, d: date, benchmark: Optional[float]) -> None:
        self.con.execute("INSERT OR REPLACE INTO equity(d,cash,equity,benchmark,invested) VALUES (?,?,?,?,?)",
                         (d.isoformat(), self.getf("cash") + self.getf("unsettled"), self.equity_now(), benchmark,
                          self.invested()))

    def trades(self, since: Optional[date] = None):
        q, a = "SELECT * FROM trades", []
        if since:
            q, a = q + " WHERE exit_date>=?", [since.isoformat()]
        return self.con.execute(q + " ORDER BY id", a).fetchall()

    def realized(self, since: Optional[date] = None) -> float:
        return sum(t["net"] for t in self.trades(since))
