"""Swing points and support/resistance zones.

No look-ahead: a swing at bar i needs k bars after it, so it only becomes KNOWN at bar i+k.
`levels_as_of(df, t)` uses swings whose confirmation bar <= t, and nothing after t."""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Swing:
    kind: str          # 'H' or 'L'
    price: float
    bar: int           # where the swing happened
    known_at: int      # first bar at which it is confirmed (= bar + k)


@dataclass
class Zone:
    low: float
    high: float
    touches: int
    last_bar: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def swing_points(df: pd.DataFrame, k: int = 3) -> list[Swing]:
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(k, len(df) - k):
        if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:i + k + 1].max():
            out.append(Swing("H", float(h[i]), i, i + k))
        if l[i] < l[i - k:i].min() and l[i] <= l[i + 1:i + k + 1].min():
            out.append(Swing("L", float(l[i]), i, i + k))
    return out


def cluster_zones(swings: list[Swing], width: float) -> list[Zone]:
    zones: list[Zone] = []
    for s in sorted(swings, key=lambda x: x.price):
        if zones and s.price - zones[-1].high <= width:
            z = zones[-1]
            z.high, z.touches, z.last_bar = max(z.high, s.price), z.touches + 1, max(z.last_bar, s.bar)
        else:
            zones.append(Zone(s.price, s.price, 1, s.bar))
    return zones


@dataclass
class Levels:
    support: list[Zone]      # below price, nearest first
    resistance: list[Zone]   # above price, nearest first


def levels_as_of(df: pd.DataFrame, t: int, atr_value: float, k: int = 3,
                 lookback: int = 250, width_atr: float = 0.5) -> Levels:
    sub = df.iloc[: t + 1]
    swings = [s for s in swing_points(sub, k) if s.known_at <= t and s.bar >= t - lookback]
    zones = cluster_zones(swings, width_atr * atr_value)
    price = float(sub["close"].iloc[-1])
    sup = sorted([z for z in zones if z.high < price], key=lambda z: price - z.high)
    res = sorted([z for z in zones if z.low > price], key=lambda z: z.low - price)
    return Levels(sup, res)
