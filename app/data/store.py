"""SQLite cache of daily candles. One row per (symbol, date)."""
from __future__ import annotations
import sqlite3
from datetime import date
from typing import Iterable, Optional
import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


class CandleStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute("""CREATE TABLE IF NOT EXISTS candles(
            symbol TEXT NOT NULL, d TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(symbol, d))""")
        self.con.commit()

    def upsert(self, symbol: str, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        self.con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)",
                             [(symbol, d, o, h, l, c, v) for d, o, h, l, c, v in rows])
        self.con.commit()
        return len(rows)

    def load(self, symbol: str, end: Optional[date] = None) -> pd.DataFrame:
        q, args = "SELECT d, open, high, low, close, volume FROM candles WHERE symbol=?", [symbol]
        if end is not None:
            q += " AND d<=?"
            args.append(end.isoformat())
        df = pd.read_sql_query(q + " ORDER BY d", self.con, params=args)
        df["d"] = pd.to_datetime(df["d"]).dt.date
        return df.set_index("d")

    def last_date(self, symbol: str) -> Optional[date]:
        r = self.con.execute("SELECT MAX(d) FROM candles WHERE symbol=?", (symbol,)).fetchone()[0]
        return date.fromisoformat(r) if r else None

    def symbols(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT symbol FROM candles")]
