"""Configuration. Fails closed: anything unclear => PAPER mode, no live orders."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Anchor runtime files to the project, never to the CWD (launchd runs with cwd=/).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


class Mode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.5        # % of equity risked per new trade
    max_open_risk_pct: float = 2.0         # aggregate open risk, % of equity
    max_positions: int = 3
    max_position_alloc_pct: float = 20.0   # per stock, % of equity
    max_sector_alloc_pct: float = 35.0
    min_reward_risk: float = 2.0           # AFTER costs
    daily_loss_limit_pct: float = 1.5
    weekly_drawdown_limit_pct: float = 4.0
    max_drawdown_pct: float = 10.0
    slippage_bps: float = 10.0             # conservative, per side
    max_adv_participation_pct: float = 1.0 # qty value <= this % of avg daily traded value
    max_chase_pct: float = 0.5             # skip if live price > entry by more than this


@dataclass(frozen=True)
class CostModel:
    """Delivery (CNC) equity charges. Defaults are PLACEHOLDERS: verify against
    Groww's current brochure + NSE circulars before trusting any P&L number."""
    brokerage_flat: float = 20.0           # per executed order (verify plan)
    brokerage_pct: float = 0.1             # lower of pct/flat (verify)
    brokerage_min: float = 5.0             # (verify)
    stt_pct: float = 0.1                   # both buy & sell for delivery
    exchange_txn_pct: float = 0.00297      # NSE cash (verify)
    sebi_pct: float = 0.0001
    stamp_duty_buy_pct: float = 0.015
    gst_pct: float = 18.0                  # on brokerage + exchange + sebi
    dp_charge_sell: float = 16.5           # per scrip sold (verify)


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.PAPER
    risk: RiskLimits = field(default_factory=RiskLimits)
    costs: CostModel = field(default_factory=CostModel)
    timezone: str = "Asia/Kolkata"
    kill_switch_file: str = str(DATA_DIR / "KILL_SWITCH")
    live_arm_file: str = str(DATA_DIR / "LIVE_ARMED")


ARM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


def load_settings(env: dict | None = None) -> Settings:
    """LIVE only if BOTH env GROWW_MODE=LIVE and the arm file holds the phrase.
    Missing / garbled / unreadable config => PAPER."""
    env = os.environ if env is None else env
    requested = str(env.get("GROWW_MODE", "PAPER")).strip().upper()
    if requested != "LIVE":
        return Settings(mode=Mode.PAPER)
    arm_path = env.get("LIVE_ARM_FILE", Settings().live_arm_file)
    try:
        with open(arm_path) as f:
            armed = f.read().strip() == ARM_PHRASE
    except OSError:
        armed = False
    return Settings(mode=Mode.LIVE if armed else Mode.PAPER)
