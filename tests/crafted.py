"""Hand-built price series that contain one specific pattern each. Clearly synthetic."""
from datetime import date
import numpy as np
import pandas as pd
from tests.synthetic import business_days


def _frame(close, vol, wick=0.004):
    n = len(close)
    days = business_days(date(2024, 1, 1), n)
    opn = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * (1 + wick),
                         "low": np.minimum(opn, close) * (1 - wick), "close": close, "volume": vol}, index=days)


def flat_index(n=300):
    df = _frame(np.full(n, 20000.0), np.full(n, 1e6))
    return df


def breakout(n=300, base_len=25, base_depth=0.06, last_vol_mult=2.5, last_jump=0.01):
    trend = n - base_len - 1
    close = 100 * np.exp(np.linspace(0, 0.45, trend))
    top = close[-1]
    t = np.arange(base_len)
    base = top * (1 - base_depth / 2 + (base_depth / 2) * np.sin(t / 3.0) * 0.9)
    close = np.r_[close, base]
    close = np.r_[close, base.max() * (1 + last_jump)]
    vol = np.full(n, 3e6); vol[-1] = 3e6 * last_vol_mult
    return _frame(close, vol)


def rs_leader(n=300, pause=10, last_vol_mult=1.6, pause_width=0.012):
    trend = n - pause - 1
    close = 100 * np.exp(np.linspace(0, 0.55, trend))
    top = close[-1]
    p = top * (1 + pause_width / 2 * np.sin(np.arange(pause) * 1.3))
    close = np.r_[close, p, p.max() * 1.006]
    vol = np.full(n, 3e6); vol[-1] *= last_vol_mult
    return _frame(close, vol, wick=0.003)


def vcp(n=300, ranges=(0.16, 0.09, 0.04), last_vol_mult=1.8):
    trend = n - 46
    close = 100 * np.exp(np.linspace(0, 0.5, trend))
    top = close[-1]
    segs, vols = [], []
    for k, r in enumerate(ranges):
        t = np.arange(15)
        segs.append(top * (1 - r / 2 + (r / 2) * np.cos(t * 2 * np.pi / 14)))
        vols.append(np.full(15, 3e6 * (1 - 0.25 * k)))
    close = np.r_[close, np.concatenate(segs), top * 1.012]
    vol = np.r_[np.full(trend, 3e6), np.concatenate(vols), 3e6 * last_vol_mult]
    return _frame(close, vol, wick=0.002)
