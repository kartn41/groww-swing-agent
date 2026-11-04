"""Persistent paper account (SQLite). Survives restarts; every event is recorded.
Nothing is committed until end_day(): a day is all-or-nothing."""
from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional


class AlreadyProcessed(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, created_on TEXT, symbol TEXT, sector TEXT,
  qty INTEGER, limit_price REAL, stop REAL, target REAL, score REAL, status TEXT, note TEXT, setup TEXT);
CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, sector TEXT, qty INTEGER, entry REAL,
  entry_date TEXT, stop REAL, target REAL, buy_cost REAL, exit_pending INTEGER DEFAULT 0, last_close REAL);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, symbol TEXT, sector TEXT, qty INTEGER,
  entry_date TEXT, entry REAL, exit_date TEXT, exit REAL, reason TEXT, gross REAL, costs REAL, net REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, d TEXT, kind TEXT, symbol TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(d TEXT PRIMARY KEY, cash REAL, equity REAL, benchmark REAL);
"""
# Columns added after the first release; added in place to existing databases.
MIGRATIONS = {"orders": {"strategy": "TEXT", "regime": "TEXT"},
              "positions": {"strategy": "TEXT", "regime": "TEXT", "risk": "REAL"},
              "trades": {"strategy": "TEXT", "regime": "TEXT", "risk": "REAL"},
              "equity": {"invested": "REAL"}}


class Ledger:
    def __init__(self, path: str, start_capital: float) -> None:
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
            for c, typ in cols.items():
                if c not in have:
                    self.con.execute(f"ALTER TABLE {table} ADD COLUMN {c} {typ}")
        if self.get("cash") is None:
            for k in ("start_capital", "cash", "peak_equity", "prev_equity"):
                self.set(k, start_capital)
        self.con.commit()

    # ---- meta ----
    def get(self, key: str) -> Optional[str]:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def getf(self, key: str) -> float:
        return float(self.get(key))

    def set(self, key: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def begin_day(self, d: date) -> None:
        last = self.get("last_processed")
        if last and date.fromisoformat(last) >= d:
            raise AlreadyProcessed(f"{d} already processed (last {last}); refusing to double-count")

    def end_day(self, d: date) -> None:
        self.set("last_processed", d.isoformat())
        self.con.commit()

    def event(self, d: date, kind: str, symbol: str = "", detail: str = "") -> None:
        self.con.execute("INSERT INTO events(ts,d,kind,symbol,detail) VALUES (?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), d.isoformat(), kind, symbol, detail))

    # ---- orders / positions / trades ----
    def add_order(self, d: date, s, qty: int, regime: str = "") -> None:
        self.con.execute(
            "INSERT INTO orders(created_on,symbol,sector,qty,limit_price,stop,target,score,status,note,setup,strategy,regime)"
            " VALUES (?,?,?,?,?,?,?,?, 'PENDING', '', ?,?,?)",
            (d.isoformat(), s.symbol, s.sector, qty, s.entry, s.stop, s.target, s.score,
             json.dumps({"evidence": s.evidence, "invalidation": s.invalidation}),
             getattr(s, "strategy", "") or "", regime))

    def pending_orders(self, before: date):
        return self.con.execute("SELECT * FROM orders WHERE status='PENDING' AND created_on<?",
                                (before.isoformat(),)).fetchall()

    def close_order(self, oid: int, status: str, note: str) -> None:
        self.con.execute("UPDATE orders SET status=?, note=? WHERE id=?", (status, note, oid))

    def positions(self):
        return self.con.execute("SELECT * FROM positions ORDER BY symbol").fetchall()

    def open_position(self, d: date, o, price: float, buy_cost: float) -> None:
        self.con.execute(
            "INSERT INTO positions(symbol,sector,qty,entry,entry_date,stop,target,buy_cost,exit_pending,last_close,"
            "strategy,regime,risk) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)",
            (o["symbol"], o["sector"], o["qty"], price, d.isoformat(), o["stop"], o["target"], buy_cost, price,
             o["strategy"], o["regime"], max(price - o["stop"], 0.0) * o["qty"] + buy_cost))

    def close_position(self, d: date, p, price: float, reason: str, sell_cost: float) -> float:
        gross = (price - p["entry"]) * p["qty"]
        costs = p["buy_cost"] + sell_cost
        net = gross - costs
        self.con.execute(
            "INSERT INTO trades(symbol,sector,qty,entry_date,entry,exit_date,exit,reason,gross,costs,net,strategy,regime,risk)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["symbol"], p["sector"], p["qty"], p["entry_date"], p["entry"], d.isoformat(), price, reason,
             gross, costs, net, p["strategy"], p["regime"], p["risk"]))
        self.con.execute("DELETE FROM positions WHERE symbol=?", (p["symbol"],))
        return net

    def update_stop(self, symbol: str, stop: float) -> None:
        self.con.execute("UPDATE positions SET stop=? WHERE symbol=?", (stop, symbol))

    def mark(self, symbol: str, close: float, exit_pending: Optional[bool] = None) -> None:
        self.con.execute("UPDATE positions SET last_close=? WHERE symbol=?", (close, symbol))
        if exit_pending is not None:
            self.con.execute("UPDATE positions SET exit_pending=? WHERE symbol=?", (int(exit_pending), symbol))

    def trades(self, since: Optional[date] = None):
        q, a = "SELECT * FROM trades", []
        if since:
            q, a = q + " WHERE exit_date>=?", [since.isoformat()]
        return self.con.execute(q + " ORDER BY id", a).fetchall()

    def invested(self) -> float:
        return sum(p["qty"] * p["last_close"] for p in self.positions())

    def equity_now(self) -> float:
        return self.getf("cash") + self.invested()

    def record_equity(self, d: date, benchmark: Optional[float]) -> None:
        self.con.execute("INSERT OR REPLACE INTO equity(d,cash,equity,benchmark,invested) VALUES (?,?,?,?,?)",
                         (d.isoformat(), self.getf("cash"), self.equity_now(), benchmark, self.invested()))
