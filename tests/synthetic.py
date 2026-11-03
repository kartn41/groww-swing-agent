"""Deterministic synthetic market for tests. Clearly NOT real data."""
from __future__ import annotations
import csv
from datetime import date, timedelta
import numpy as np
from app.data.store import CandleStore


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_series(days, seed: int, drift: float, vol: float, start_px: float, pullbacks: bool = True):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, len(days))
    # periodic pullbacks so the strategy has something to find
    for k in (range(60, len(days), 45) if pullbacks else []):
        rets[k:k + 5] -= 0.012
        rets[k + 5] += 0.02
    close = start_px * np.exp(np.cumsum(rets))
    opn = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, vol / 4, len(days)))
    hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, vol / 2, len(days))))
    lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, vol / 2, len(days))))
    v = rng.integers(2_000_000, 6_000_000, len(days)).astype(float)
    return [(d.isoformat(), float(o), float(h), float(l), float(c), float(x))
            for d, o, h, l, c, x in zip(days, opn, hi, lo, close, v)]


def build(tmpdir: str, n_days: int = 420, n_stocks: int = 50, seed: int = 7):
    days = business_days(date(2025, 1, 1), n_days)
    store = CandleStore(f"{tmpdir}/market.db")
    store.upsert("NSE-NIFTY", make_series(days, seed, 0.0009, 0.007, 22000, pullbacks=False))
    syms = []
    for i in range(n_stocks):
        sym = f"STK{i:02d}"
        syms.append(sym)
        store.upsert(f"NSE-{sym}", make_series(days, seed + i + 1, 0.0004 + 0.00003 * (i % 10),
                                               0.012 + 0.001 * (i % 5), 200 + 37 * i))
    uni = f"{tmpdir}/ind_nifty50list.csv"
    with open(uni, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Company Name", "Industry", "Symbol", "Series", "ISIN Code"])
        for i, s in enumerate(syms):
            w.writerow([f"{s} Ltd", f"Sector{i % 8}", s, "EQ", f"INE{i:09d}"])
    return store, uni, days
