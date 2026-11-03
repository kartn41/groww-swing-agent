"""NIFTY 50 universe from NSE's official constituents CSV (download from niftyindices.com,
'NIFTY 50' -> 'Index Constituents' -> ind_nifty50list.csv). Not hardcoded: refresh it
whenever the index is rebalanced. Fails closed on a missing or malformed file."""
from __future__ import annotations
import csv
import os
import time
from dataclasses import dataclass


class UniverseError(Exception):
    pass


@dataclass(frozen=True)
class Constituent:
    symbol: str
    sector: str


def load_universe(path: str, min_count: int = 45, max_count: int = 55,
                  stale_days: int = 45, now: float | None = None) -> tuple[list[Constituent], list[str]]:
    if not os.path.exists(path):
        raise UniverseError(f"universe file missing: {path} (download ind_nifty50list.csv from niftyindices.com)")
    warnings = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        if "symbol" not in cols or "industry" not in cols:
            raise UniverseError(f"unexpected columns {reader.fieldnames}; need Symbol and Industry")
        out, seen = [], set()
        for row in reader:
            sym = (row[cols["symbol"]] or "").strip().upper()
            series = (row.get(cols.get("series", ""), "EQ") or "EQ").strip().upper()
            if not sym or series != "EQ" or sym in seen:
                continue
            seen.add(sym)
            out.append(Constituent(sym, (row[cols["industry"]] or "Unknown").strip()))
    if not (min_count <= len(out) <= max_count):
        raise UniverseError(f"universe has {len(out)} symbols; expected ~50")
    age_days = ((now or time.time()) - os.path.getmtime(path)) / 86400
    if age_days > stale_days:
        warnings.append(f"universe file is {age_days:.0f} days old; re-download if NIFTY 50 was rebalanced")
    return out, warnings
