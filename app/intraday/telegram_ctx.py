"""Load intraday pick/exit context from the ledger for Telegram and markdown reports."""
from __future__ import annotations
import json
from datetime import date

from app.paper.ledger import Ledger


def picks_on_day(ledger: Ledger, d: date) -> list[dict]:
    rows = ledger.con.execute(
        "SELECT symbol, detail FROM events WHERE d=? AND kind='FILL' ORDER BY id",
        (d.isoformat(),),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        sym = r["symbol"]
        try:
            data = json.loads(r["detail"])
            if isinstance(data, dict):
                data.setdefault("symbol", sym)
                out.append(data)
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        out.append({"symbol": sym, "summary": r["detail"]})
    return out


def picks_between(ledger: Ledger, start: date, end: date) -> list[tuple[date, dict]]:
    rows = ledger.con.execute(
        "SELECT d, symbol, detail FROM events WHERE kind='FILL' AND d>=? AND d<=? ORDER BY d, id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out: list[tuple[date, dict]] = []
    for r in rows:
        d = date.fromisoformat(r["d"])
        sym = r["symbol"]
        try:
            data = json.loads(r["detail"])
            if isinstance(data, dict):
                data.setdefault("symbol", sym)
                out.append((d, data))
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        out.append((d, {"symbol": sym, "summary": r["detail"]}))
    return out
