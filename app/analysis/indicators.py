"""Indicators. Every value at row t uses only rows <= t."""
from __future__ import annotations
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    rs = up / dn
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def relative_volume(vol: pd.Series, n: int = 20) -> pd.Series:
    """Today's volume vs the average of the PREVIOUS n days (today excluded)."""
    return vol / vol.shift(1).rolling(n, min_periods=n).mean()


def roc(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1
