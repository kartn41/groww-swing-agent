"""GoldBEES / SilverBEES swing settings. Every number is a starting hypothesis to validate by backtest,
not a proven value. All of them can be overridden from .env (ETF_<NAME>, ETF_<SYMBOL>_<NAME>)."""
from __future__ import annotations
import os
from dataclasses import dataclass, field, fields, replace
from datetime import time

from app.config import CostModel

SYMBOLS = ("GOLDBEES", "SILVERBEES")


@dataclass(frozen=True)
class Profile:
    """Per-ETF behaviour. SilverBEES is more volatile: deeper dips, smaller size, tighter loss budget."""
    symbol: str
    max_exposure_pct: float          # of strategy equity held in this ETF
    max_loss_pct: float              # loss at the stop for the whole position (all tranches), % of equity
    min_dip_pct: float               # pullback from the 20-day high, %
    min_dip_atr: float               # ... and in ATRs
    rsi_dip: float                   # RSI must have dipped to <= this in the last 5 sessions
    stop_atr_min: float
    stop_atr_max: float
    target_atr: float                # preferred target = max(preferred_target_pct, target_atr x ATR%)
    trail_atr: float
    sudden_drop_pct: float           # intraday fall from previous close / day high that forces a sale
    sudden_drop_atr: float           # ... or this many ATRs, whichever is larger
    max_hold_days: int
    slippage_bps: float              # per side, includes half the bid-ask spread
    max_spread_bps: float


GOLD = Profile("GOLDBEES", max_exposure_pct=60.0, max_loss_pct=1.0, min_dip_pct=1.0, min_dip_atr=1.0,
               rsi_dip=55.0, stop_atr_min=1.5, stop_atr_max=2.5, target_atr=2.0, trail_atr=1.5,
               sudden_drop_pct=2.5, sudden_drop_atr=2.0, max_hold_days=25, slippage_bps=5.0, max_spread_bps=15.0)
SILVER = Profile("SILVERBEES", max_exposure_pct=45.0, max_loss_pct=0.75, min_dip_pct=2.0, min_dip_atr=1.0,
                 rsi_dip=52.0, stop_atr_min=1.5, stop_atr_max=2.25, target_atr=2.0, trail_atr=1.5,
                 sudden_drop_pct=4.0, sudden_drop_atr=2.0, max_hold_days=20, slippage_bps=10.0, max_spread_bps=25.0)
# Aggressive preset: more capital deployed, shallower dips, trend entries in bull regimes (validate with backtest).
GOLD_AGGR = Profile("GOLDBEES", max_exposure_pct=85.0, max_loss_pct=1.25, min_dip_pct=0.5, min_dip_atr=0.75,
                    rsi_dip=58.0, stop_atr_min=1.25, stop_atr_max=2.75, target_atr=2.5, trail_atr=2.0,
                    sudden_drop_pct=2.5, sudden_drop_atr=2.0, max_hold_days=40, slippage_bps=5.0, max_spread_bps=15.0)
SILVER_AGGR = Profile("SILVERBEES", max_exposure_pct=55.0, max_loss_pct=1.0, min_dip_pct=1.5, min_dip_atr=0.75,
                      rsi_dip=55.0, stop_atr_min=1.25, stop_atr_max=2.5, target_atr=2.5, trail_atr=2.0,
                      sudden_drop_pct=4.0, sudden_drop_atr=2.0, max_hold_days=30, slippage_bps=10.0, max_spread_bps=25.0)


def etf_costs() -> CostModel:
    """Gold/silver ETFs are not equity-oriented funds: no STT. Other delivery charges as for stocks.
    Placeholder rates: verify against Groww's current charges page before trusting P&L."""
    return CostModel(stt_pct=0.0)


@dataclass(frozen=True)
class EtfConfig:
    strategy_mode: str = "balanced"            # balanced | aggressive | momentum | conservative (ETF_STRATEGY_MODE)
    core_pct_strong: float = 55.0              # momentum: target % of equity in ETF when STRONG_BULL
    core_pct_mild: float = 40.0                # momentum: target when MILD_BULL
    core_pct_sideways: float = 30.0            # momentum: still hold a slice when trend is chopping but not bearish
    momentum_chase_pct: float = 3.5            # momentum: buy limit may be this far above the signal close (gap-up days)
    near_200_tolerance_pct: float = 3.0        # momentum: within this % below EMA200 still counts as correction not hard bear
    rebound_drop_pct: float = 1.2              # momentum: buy green day after prior session fell at least this much
    momentum_day_pct: float = 1.0              # momentum: add on session after prior day rose at least this much
    symbols: str = "GOLDBEES,SILVERBEES"      # ETF_SYMBOLS=GOLDBEES to trade gold only
    capital: float = 100000.0
    max_alloc_pct: float = 100.0             # strategy capital that may ever be invested
    max_combined_pct: float = 80.0           # both ETFs together (they are correlated)
    tranche_weights: tuple = (0.4, 0.3, 0.3)
    min_target_pct: float = 1.5
    preferred_target_pct: float = 2.0
    extended_target_pct: float = 2.5
    min_net_rr: float = 0.8                  # net reward/risk at the preferred target; dip signals hit
                                             # +2 ATR before -2 ATR ~3x as often (2021-24 GoldBEES study)
    min_days_between_tranches: int = 2
    add_gap_atr: float = 0.75                # a dip add needs price >= this many ATRs below the last tranche
    daily_loss_limit_pct: float = 1.5
    max_drawdown_pct: float = 8.0
    cooldown_days: int = 3                   # no re-entry after a stop / sudden-drop exit
    max_chase_pct: float = 0.3              # skip if the next open is this far above the limit
    entry_headroom_pct: float = 0.0          # limit = signal close + this (a limit AT the close only fills
                                             # when price falls back: adverse selection)
    extra_confirmations: int = -1            # added to the per-regime requirement (may be negative)
    exit_on_support_lost: bool = True
    exit_on_macd_cross: bool = True
    exit_on_rsi_overbought: bool = True
    same_day_proceeds_pct: float = 0.0       # share of today's sale proceeds usable today (Groww: verify)
    window_start: time = time(9, 45)
    window_end: time = time(15, 0)
    profiles: dict = field(default_factory=lambda: {"GOLDBEES": GOLD, "SILVERBEES": SILVER})
    costs: CostModel = field(default_factory=etf_costs)

    def profile(self, symbol: str) -> Profile:
        return self.profiles[symbol]

    @property
    def symbol_list(self) -> list[str]:
        return [x.strip().upper() for x in self.symbols.split(",") if x.strip()]


def _cast(old, raw: str):
    if isinstance(old, bool):
        return raw.strip().lower() in ("1", "true", "on", "yes")
    if isinstance(old, int):
        return int(raw)
    if isinstance(old, float):
        return float(raw)
    if isinstance(old, time):
        h, m = raw.strip().split(":")
        return time(int(h), int(m))
    if isinstance(old, tuple):
        return tuple(float(x) for x in raw.split(","))
    return raw


def load_etf_config(env=None) -> EtfConfig:
    env = os.environ if env is None else env
    base = EtfConfig()
    kw = {}
    for f in fields(EtfConfig):
        if f.name in ("profiles", "costs"):
            continue
        raw = env.get(f"ETF_{f.name.upper()}")
        if raw not in (None, ""):
            kw[f.name] = _cast(getattr(base, f.name), raw)
    profiles = {}
    for sym, prof in base.profiles.items():
        pk = {}
        for f in fields(Profile):
            raw = env.get(f"ETF_{sym}_{f.name.upper()}")
            if f.name != "symbol" and raw not in (None, ""):
                pk[f.name] = _cast(getattr(prof, f.name), raw)
        profiles[sym] = replace(prof, **pk)
    cfg = replace(base, profiles=profiles, **kw)
    bad = [x for x in cfg.symbol_list if x not in SYMBOLS]
    if bad or not cfg.symbol_list:
        raise ValueError(f"ETF_SYMBOLS may only contain {SYMBOLS}, got {cfg.symbols!r}")
    w = cfg.tranche_weights
    if len(w) != 3 or abs(sum(w) - 1.0) > 1e-6 or min(w) <= 0:
        raise ValueError(f"ETF_TRANCHE_WEIGHTS must be three positive numbers summing to 1, got {w}")
    mode = str(cfg.strategy_mode or "balanced").strip().lower()
    if mode not in ("balanced", "aggressive", "momentum", "conservative"):
        raise ValueError(f"ETF_STRATEGY_MODE must be balanced, aggressive, momentum, or conservative, "
                         f"got {cfg.strategy_mode!r}")
    if mode in ("aggressive", "momentum"):
        aggr_profiles = {}
        for sym, balanced in (("GOLDBEES", GOLD), ("SILVERBEES", SILVER)):
            base_p = GOLD_AGGR if sym == "GOLDBEES" else SILVER_AGGR
            pk = {}
            for f in fields(Profile):
                raw = env.get(f"ETF_{sym}_{f.name.upper()}")
                if f.name != "symbol" and raw not in (None, ""):
                    pk[f.name] = _cast(getattr(balanced, f.name), raw)
            aggr_profiles[sym] = replace(base_p, **pk)
        kw_mode = dict(
            max_combined_pct=95.0 if cfg.max_combined_pct == 80.0 else cfg.max_combined_pct,
            tranche_weights=(0.55, 0.25, 0.2) if cfg.tranche_weights == (0.4, 0.3, 0.3) else cfg.tranche_weights,
            min_days_between_tranches=1 if cfg.min_days_between_tranches == 2 else cfg.min_days_between_tranches,
            extended_target_pct=max(cfg.extended_target_pct, 3.5),
            profiles={**cfg.profiles, **aggr_profiles},
        )
        if mode == "aggressive":
            cfg = replace(cfg, strategy_mode="aggressive",
                          min_net_rr=0.65 if cfg.min_net_rr == 0.8 else cfg.min_net_rr, **kw_mode)
        else:
            mo = {"strategy_mode": "momentum",
                  "min_net_rr": 0.55 if cfg.min_net_rr == 0.8 else cfg.min_net_rr,
                  "max_chase_pct": 3.5 if cfg.max_chase_pct == 0.3 else cfg.max_chase_pct,
                  **kw_mode}
            if env.get("ETF_EXIT_ON_RSI_OVERBOUGHT") in (None, ""):
                mo["exit_on_rsi_overbought"] = False
            cfg = replace(cfg, **mo)
    if mode == "conservative":
        cfg = replace(
            cfg,
            strategy_mode="conservative",
            core_pct_strong=30.0 if cfg.core_pct_strong == 55.0 else cfg.core_pct_strong,
            core_pct_mild=0.0 if cfg.core_pct_mild == 40.0 else cfg.core_pct_mild,
            core_pct_sideways=0.0 if cfg.core_pct_sideways == 30.0 else cfg.core_pct_sideways,
            min_net_rr=1.0 if cfg.min_net_rr == 0.8 else cfg.min_net_rr,
            max_combined_pct=60.0 if cfg.max_combined_pct == 80.0 else cfg.max_combined_pct,
            max_drawdown_pct=5.0 if cfg.max_drawdown_pct == 8.0 else cfg.max_drawdown_pct,
            daily_loss_limit_pct=1.2 if cfg.daily_loss_limit_pct == 1.5 else cfg.daily_loss_limit_pct,
            momentum_chase_pct=1.5 if cfg.momentum_chase_pct == 3.5 else cfg.momentum_chase_pct,
        )
    return replace(cfg, strategy_mode=mode)
