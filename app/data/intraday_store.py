"""SQLite cache of intraday candles. Primary key (symbol, d) where d is 'YYYY-MM-DD HH:MM:SS' IST."""
from __future__ import annotations
import sqlite3
from datetime import date, datetime
from typing import Iterable, Optional
import pandas as pd


class IntradayStore:
    def __init__(self, path: str) -> None:
        self.con = sqlite3.connect(path)
        self.con.execute("""CREATE TABLE IF NOT EXISTS candles(
            symbol TEXT NOT NULL, d TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(symbol, d))""")
        self.con.commit()

    def upsert(self, symbol: str, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        self.con.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)",
            [(symbol, d, o, h, l, c, v) for d, o, h, l, c, v in rows],
        )
        self.con.commit()
        return len(rows)

    def load(self, symbol: str, day: Optional[date] = None, end_ts: Optional[str] = None) -> pd.DataFrame:
        q, args = "SELECT d, open, high, low, close, volume FROM candles WHERE symbol=?", [symbol]
        if day is not None:
            q += " AND d>=? AND d<?"
            args.extend([f"{day.isoformat()} 09:00:00", f"{day.isoformat()} 23:59:59"])
        if end_ts is not None:
            q += " AND d<=?"
            args.append(end_ts)
        df = pd.read_sql_query(q + " ORDER BY d", self.con, params=args)
        if df.empty:
            return df
        df["d"] = pd.to_datetime(df["d"])
        return df.set_index("d")

    def session_times(self, day: date, symbol: str = "NSE-NIFTY") -> list[datetime]:
        rows = self.con.execute(
            "SELECT d FROM candles WHERE symbol=? AND d>=? AND d<=? ORDER BY d",
            (symbol, f"{day.isoformat()} 09:00:00", f"{day.isoformat()} 16:00:00"),
        ).fetchall()
        return [datetime.fromisoformat(r[0]) for r in rows]
