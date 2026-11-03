"""Deterministic risk engine. No LLM output may bypass anything in here."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional
from app.config import RiskLimits, CostModel
from app.risk.costs import order_charges, round_trip_cost


@dataclass
class TradePlan:
    symbol: str
    sector: str
    entry: float
    stop: float
    target: float


@dataclass
class OpenPosition:
    symbol: str
    sector: str
    qty: int
    entry: float
    stop: float


@dataclass
class PortfolioState:
    equity: float
    cash: float
    positions: list[OpenPosition] = field(default_factory=list)
    realized_pnl_today: float = 0.0
    unrealized_pnl_today: float = 0.0     # mark-to-market change today on open positions
    week_start_equity: Optional[float] = None
    peak_equity: Optional[float] = None
    entries_disabled: bool = False       # kill switch / manual
    data_ok: bool = True                 # False => fail closed


@dataclass
class Decision:
    approved: bool
    qty: int = 0
    reasons: list[str] = field(default_factory=list)   # rejection reasons
    risk_amount: float = 0.0
    net_reward_risk: float = 0.0


def open_risk(p: PortfolioState) -> float:
    return sum(max(pos.entry - pos.stop, 0) * pos.qty for pos in p.positions)


def evaluate(plan: TradePlan, p: PortfolioState, lim: RiskLimits, costs: CostModel,
             avg_daily_value: float = float("inf")) -> Decision:
    d = Decision(approved=False)
    r = d.reasons

    if not p.data_ok:
        r.append("critical data stale/unavailable (fail closed)")
    if p.entries_disabled:
        r.append("new entries disabled (kill switch)")
    if p.equity <= 0:
        r.append("non-positive equity")
    if not (plan.stop < plan.entry < plan.target):
        r.append("invalid plan: need stop < entry < target for a long")
    if r:
        return d

    if len(p.positions) >= lim.max_positions:
        r.append(f"max positions reached ({lim.max_positions})")
    if any(pos.symbol == plan.symbol for pos in p.positions):
        r.append("already holding symbol (no averaging down/pyramiding)")
    if -(p.realized_pnl_today + p.unrealized_pnl_today) >= p.equity * lim.daily_loss_limit_pct / 100:
        r.append("daily loss limit reached")
    if p.week_start_equity and (p.week_start_equity - p.equity) >= p.week_start_equity * lim.weekly_drawdown_limit_pct / 100:
        r.append("weekly drawdown limit reached")
    if p.peak_equity and (p.peak_equity - p.equity) >= p.peak_equity * lim.max_drawdown_pct / 100:
        r.append("max portfolio drawdown reached")
    if r:
        return d

    slip = plan.entry * lim.slippage_bps / 10_000
    eff_entry = plan.entry + slip           # assume worse fill on entry
    eff_stop = plan.stop - slip             # and worse fill on the stop
    per_share_risk = eff_entry - eff_stop
    if per_share_risk <= 0:
        r.append("per-share risk <= 0")
        return d

    budget = p.equity * lim.risk_per_trade_pct / 100
    remaining_open_budget = p.equity * lim.max_open_risk_pct / 100 - open_risk(p)
    if remaining_open_budget <= 0:
        r.append("aggregate open-risk limit reached")
        return d
    budget = min(budget, remaining_open_budget)

    qty = math.floor(budget / per_share_risk)
    qty = min(qty, math.floor(p.cash / eff_entry))
    qty = min(qty, math.floor(p.equity * lim.max_position_alloc_pct / 100 / eff_entry))

    sector_val = sum(x.qty * x.entry for x in p.positions if x.sector == plan.sector)
    sector_room = p.equity * lim.max_sector_alloc_pct / 100 - sector_val
    qty = min(qty, math.floor(max(sector_room, 0) / eff_entry))

    if math.isfinite(avg_daily_value):
        qty = min(qty, math.floor(avg_daily_value * lim.max_adv_participation_pct / 100 / eff_entry))

    # Cash must cover the order AND its buy-side charges.
    while qty > 0 and qty * eff_entry + order_charges(qty * eff_entry, "BUY", costs) > p.cash:
        qty -= 1

    if qty <= 0:
        r.append("computed quantity is zero after caps")
        return d

    # Reward/risk NET of costs; a stop-out also pays round-trip costs.
    cost_target = round_trip_cost(eff_entry, plan.target - slip, qty, costs)
    cost_stop = round_trip_cost(eff_entry, eff_stop, qty, costs)
    net_reward = (plan.target - slip - eff_entry) * qty - cost_target
    net_risk = per_share_risk * qty + cost_stop
    nrr = net_reward / net_risk
    d.net_reward_risk = nrr
    if nrr < lim.min_reward_risk:
        r.append(f"net reward/risk {nrr:.2f} < {lim.min_reward_risk}")
        return d

    d.approved, d.qty, d.risk_amount = True, qty, net_risk
    return d
