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


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder's ADX with +DI / -DI."""
    up, dn = df["high"].diff(), -df["low"].diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = _wilder(true_range(df), n)
    pdi = 100 * _wilder(plus_dm, n) / tr
    mdi = 100 * _wilder(minus_dm, n) / tr
    dx = (100 * (pdi - mdi).abs() / (pdi + mdi)).fillna(0.0)
    return pd.DataFrame({"adx": _wilder(dx, n), "pdi": pdi, "mdi": mdi})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": up, "lower": lo, "pct_b": (close - lo) / (up - lo)})
