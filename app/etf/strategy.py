"""GoldBEES / SilverBEES dip-buying rules. Pure functions: the backtest and the paper/live cycle call the
same code. Decisions use the completed daily bar only; execution happens in the next session."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional

from app.etf.analysis import (BEARISH_MOMENTUM, HEALTHY_CORRECTION, MILD_BEAR, MILD_BULL, NO_PULLBACK,
                              POSSIBLE_REVERSAL, SIDEWAYS, STRONG_BEAR, STRONG_BULL, UPTREND_PULLBACK, Snapshot)
from app.etf.config import EtfConfig, Profile
from app.risk.costs import order_charges, round_trip_cost

# regime -> (allocation factor, confirmations needed, pullback types that may be bought)
BALANCED_RULES = {
    STRONG_BULL: (1.0, 2, {UPTREND_PULLBACK, HEALTHY_CORRECTION}),
    MILD_BULL: (0.8, 3, {UPTREND_PULLBACK, HEALTHY_CORRECTION}),
    SIDEWAYS: (0.6, 3, {UPTREND_PULLBACK, HEALTHY_CORRECTION, POSSIBLE_REVERSAL}),
    MILD_BEAR: (0.4, 4, {POSSIBLE_REVERSAL}),
    STRONG_BEAR: (0.0, 99, set()),
}
AGGRESSIVE_RULES = {
    STRONG_BULL: (1.0, 1, {UPTREND_PULLBACK, HEALTHY_CORRECTION, NO_PULLBACK}),
    MILD_BULL: (0.9, 2, {UPTREND_PULLBACK, HEALTHY_CORRECTION, NO_PULLBACK}),
    SIDEWAYS: (0.5, 3, {UPTREND_PULLBACK, HEALTHY_CORRECTION}),
    MILD_BEAR: (0.0, 99, set()),
    STRONG_BEAR: (0.0, 99, set()),
}
MOMENTUM_RULES = {
    STRONG_BULL: (1.0, 1, {UPTREND_PULLBACK, HEALTHY_CORRECTION, NO_PULLBACK, POSSIBLE_REVERSAL}),
    MILD_BULL: (0.9, 2, {UPTREND_PULLBACK, HEALTHY_CORRECTION, NO_PULLBACK, POSSIBLE_REVERSAL}),
    SIDEWAYS: (0.5, 2, {UPTREND_PULLBACK, HEALTHY_CORRECTION, POSSIBLE_REVERSAL}),
    MILD_BEAR: (0.35, 3, {POSSIBLE_REVERSAL}),
    STRONG_BEAR: (0.0, 99, set()),
}


def regime_rules(cfg: EtfConfig):
    if cfg.strategy_mode == "momentum":
        return MOMENTUM_RULES
    if cfg.strategy_mode == "aggressive":
        return AGGRESSIVE_RULES
    return BALANCED_RULES


def trading_regime(s: Snapshot, cfg: EtfConfig) -> str:
    """Momentum entries: a pullback below short EMAs while still near/above the 200d trend is a correction, not a no-buy bear."""
    if cfg.strategy_mode != "momentum":
        return s.regime
    floor = s.ema[200] * (1 - cfg.near_200_tolerance_pct / 100)
    if s.close < floor:
        return s.regime
    if s.regime == STRONG_BEAR:
        if s.close > s.ema[50] and s.slope50 > 0 and s.hist > 0:
            return MILD_BULL
        return SIDEWAYS
    if s.regime == MILD_BEAR and (s.pullback == POSSIBLE_REVERSAL or s.hist >= s.hist_prev):
        return SIDEWAYS
    return s.regime


def core_target_fraction(regime: str, cfg: EtfConfig) -> float:
    if cfg.strategy_mode != "momentum":
        return 0.0
    if regime == STRONG_BULL:
        return cfg.core_pct_strong / 100
    if regime == MILD_BULL:
        return cfg.core_pct_mild / 100
    if regime == SIDEWAYS:
        return cfg.core_pct_sideways / 100
    return 0.0


def plan_limit(close: float, kind: str, cfg: EtfConfig) -> float:
    """Next-session buy limit. Core/top-ups use a wide cap for gap-up days; tactical entries use a smaller chase."""
    if cfg.strategy_mode == "momentum" and kind == "CORE_TOPUP":
        return round(close * (1 + cfg.momentum_chase_pct / 100), 2)
    if cfg.strategy_mode == "momentum" and kind in ("MOMENTUM_INITIAL", "REBOUND_INITIAL", "TREND_INITIAL"):
        return round(close * (1 + max(cfg.entry_headroom_pct, 1.0) / 100), 2)
    return round(close * (1 + cfg.entry_headroom_pct / 100), 2)


def _rebound_after_drop(s: Snapshot, cfg: EtfConfig) -> tuple[bool, list[str]]:
    if cfg.strategy_mode != "momentum":
        return False, []
    tr = trading_regime(s, cfg)
    if tr not in (STRONG_BULL, MILD_BULL, SIDEWAYS):
        return False, []
    drop = s.prev_day_pct
    if drop > -cfg.rebound_drop_pct / 100 and s.moves.get(3, 0) > -2 * cfg.rebound_drop_pct / 100:
        return False, []
    if s.close <= s.open or s.close <= s.prev_close or s.close < s.ema[20]:
        return False, []
    if s.rsi >= 72:
        return False, []
    why = (f"rebound after {drop:+.1%} prior session"
           if drop <= -cfg.rebound_drop_pct / 100 else f"rebound after {s.moves[3]:+.1%} over 3 sessions")
    return True, [why + f" (selloff >= {cfg.rebound_drop_pct:.1f}%)",
                  f"green close {s.close:.2f} above EMA20 {s.ema[20]:.2f}"]


def _momentum_follow_through(s: Snapshot, cfg: EtfConfig) -> tuple[bool, list[str]]:
    if cfg.strategy_mode != "momentum":
        return False, []
    if trading_regime(s, cfg) not in (STRONG_BULL, MILD_BULL):
        return False, []
    if s.prev_day_pct < cfg.momentum_day_pct / 100:
        return False, []
    if not (s.hist > 0 and s.close > s.ema[9] > s.ema[20]):
        return False, []
    if s.rsi >= 75:
        return False, []
    return True, [f"follow-through after prior session {s.prev_day_pct:+.1%}",
                  f"close above EMA9/20, MACD hist positive"]
REGIME_WEIGHT = {STRONG_BULL: 1.0, MILD_BULL: 0.85, SIDEWAYS: 0.7, MILD_BEAR: 0.5, STRONG_BEAR: 0.0}


@dataclass
class PositionView:
    symbol: str
    qty: int
    avg: float
    stop: float
    target: float
    tranches: int
    last_tranche_price: float
    sessions_since_tranche: int
    sessions_held: int
    peak_close: float
    planned_value: float
    extended: bool = False


@dataclass
class Targets:
    minimum: float      # fractions, e.g. 0.015
    preferred: float
    extended: float


@dataclass
class EntrySignal:
    symbol: str
    tranche: int
    kind: str                     # INITIAL / DIP_ADD / CONTINUATION_ADD
    limit: float
    stop: float
    targets: Targets
    score: float
    regime: str
    pullback: str
    reasons: list = field(default_factory=list)


@dataclass
class Rejection:
    symbol: str
    reason: str


@dataclass
class Sizing:
    qty: int = 0
    reasons: list = field(default_factory=list)
    net_rr: float = 0.0
    risk_amount: float = 0.0
    new_avg: float = 0.0
    new_stop: float = 0.0
    planned_value: float = 0.0


@dataclass
class Review:
    exit_reason: Optional[str] = None
    stop: float = 0.0
    target: float = 0.0
    extended: bool = False
    notes: list = field(default_factory=list)


def targets_for(s: Snapshot, prof: Profile, cfg: EtfConfig) -> Targets:
    """Dynamic targets: the configured percentages are floors; volatile markets get wider targets."""
    pref = max(cfg.preferred_target_pct / 100, prof.target_atr * s.atr_pct)
    return Targets(minimum=max(cfg.min_target_pct / 100, 0.67 * pref),
                   preferred=pref,
                   extended=max(cfg.extended_target_pct / 100, pref + s.atr_pct))


def breakeven_frac(prof: Profile, cfg: EtfConfig, value: float = 50000.0) -> float:
    """Round-trip costs + slippage as a fraction of position value."""
    return 2 * prof.slippage_bps / 1e4 + round_trip_cost(value, value, 1, cfg.costs) / value


def _trend_rally(s: Snapshot, aggressive: bool, cfg: EtfConfig) -> tuple[bool, list[str]]:
    """Bull trend entry without a deep dip (aggressive mode only)."""
    tr = trading_regime(s, cfg) if aggressive else s.regime
    if not aggressive or tr not in (STRONG_BULL, MILD_BULL):
        return False, []
    e = s.ema
    stacked = s.close > e[20] > e[50] > e[200]
    momentum = s.slope50 > 0 and s.adx >= 18 and s.hist > 0 and s.roc10 > 0 and s.pdi >= s.mdi
    if not (stacked and momentum):
        return False, []
    if s.rsi >= 68 or s.pct_b > 0.95:
        return False, []
    if s.regime == MILD_BULL and s.depth_atr < 0.5 and s.rsi > 62:
        return False, []
    reasons = [
        f"trend rally: EMA stack bull, EMA50 {s.slope50:+.1%}, ADX {s.adx:.0f}, ROC10 {s.roc10:+.1%}",
        f"MACD hist {s.hist:+.3f} positive, %b {s.pct_b:.2f}",
    ]
    return True, reasons


def _near_support(s: Snapshot) -> Optional[str]:
    for name, lvl in (("EMA20", s.ema[20]), ("EMA50", s.ema[50]), ("lower Bollinger band", s.bb_lower),
                      ("EMA200", s.ema[200])):
        if abs(s.pullback_low - lvl) <= 1.0 * s.atr:
            return f"pullback low {s.pullback_low:.2f} at {name} {lvl:.2f}"
    return None


def entry_signal(s: Snapshot, prof: Profile, cfg: EtfConfig, pos: Optional[PositionView] = None,
                 cooldown: bool = False) -> EntrySignal | Rejection:
    sym = s.symbol
    tr = trading_regime(s, cfg)
    rules = regime_rules(cfg)
    factor, need, allowed = rules[tr]
    aggressive = cfg.strategy_mode in ("aggressive", "momentum")
    need = max(1, need + cfg.extra_confirmations)
    if s.vol_shock:
        need += 1
    if cooldown:
        return Rejection(sym, f"cool-down after a protective exit ({cfg.cooldown_days} sessions)")
    if factor <= 0:
        return Rejection(sym, f"regime {tr} (chart {s.regime}): no new purchases, preserving capital")
    if pos is not None and pos.tranches >= 3:
        return Rejection(sym, "all 3 tranches already bought")
    if pos is not None and tr in (MILD_BEAR, STRONG_BEAR):
        return Rejection(sym, f"regime {tr}: no averaging down in a bearish trend")
    if pos is not None and pos.sessions_since_tranche < cfg.min_days_between_tranches:
        return Rejection(sym, f"last tranche {pos.sessions_since_tranche} session(s) ago "
                              f"(< {cfg.min_days_between_tranches})")
    if s.pullback == BEARISH_MOMENTUM:
        return Rejection(sym, "strong bearish momentum, no reversal confirmation yet")
    n = len(s.confirmations)

    kind, reasons = "INITIAL", []
    trend, trend_reasons = _trend_rally(s, aggressive, cfg)
    rebound, rebound_reasons = _rebound_after_drop(s, cfg)
    momentum, momentum_reasons = _momentum_follow_through(s, cfg)
    if pos is None:
        if trend and s.pullback in allowed:
            kind = "TREND_INITIAL"
            reasons = trend_reasons
        elif rebound:
            kind = "REBOUND_INITIAL"
            reasons = rebound_reasons
        elif momentum:
            kind = "MOMENTUM_INITIAL"
            reasons = momentum_reasons
        elif s.pullback not in allowed:
            return Rejection(sym, f"{s.pullback} is not a buyable setup in a {s.regime} market")
        else:
            if s.depth_pct < prof.min_dip_pct / 100 or s.depth_atr < prof.min_dip_atr:
                return Rejection(sym, f"pullback {s.depth_pct:.1%} ({s.depth_atr:.1f} ATR) from 20d high {s.swing_high:.2f} "
                                      f"is shallower than {prof.min_dip_pct}% / {prof.min_dip_atr} ATR")
            if s.rsi_min5 > prof.rsi_dip:
                return Rejection(sym, f"RSI did not dip (5-day low {s.rsi_min5:.0f} > {prof.rsi_dip:.0f})")
            sup = _near_support(s)
            if sup is None:
                return Rejection(sym, f"pullback low {s.pullback_low:.2f} is not near EMA20/50/200 or the lower band")
            if s.regime == SIDEWAYS and s.pullback_low > s.bb_lower + 0.5 * s.atr and s.rsi_min5 > 40:
                return Rejection(sym, "sideways market: dip did not reach the lower band / RSI 40")
            reasons += [f"pullback {s.depth_pct:.1%} ({s.depth_atr:.1f} ATR) from 20d high {s.swing_high:.2f}; {sup}",
                        f"RSI dipped to {s.rsi_min5:.0f}, now {s.rsi:.0f}"]
    else:
        if s.close <= pos.stop + 0.5 * s.atr:
            return Rejection(sym, f"price {s.close:.2f} too close to the stop {pos.stop:.2f}")
        if s.close <= pos.last_tranche_price - cfg.add_gap_atr * s.atr:
            kind = "DIP_ADD"
            reasons.append(f"{(pos.last_tranche_price - s.close) / s.atr:.1f} ATR below the last tranche "
                           f"{pos.last_tranche_price:.2f}, uptrend intact")
        elif s.close > pos.avg and s.depth_atr >= 1.0 and s.pullback in allowed:
            kind = "CONTINUATION_ADD"
            reasons.append(f"position in profit ({s.close / pos.avg - 1:+.1%}); fresh {s.depth_atr:.1f} ATR "
                           "pullback bought back")
        else:
            return Rejection(sym, "no new favourable price for the next tranche (no blind averaging)")
    rsi_cap = 74 if kind in ("TREND_INITIAL", "MOMENTUM_INITIAL", "REBOUND_INITIAL") else 62
    if s.rsi > rsi_cap:
        return Rejection(sym, f"RSI {s.rsi:.0f} already extended (> {rsi_cap})")
    if n < need:
        return Rejection(sym, f"only {n} reversal confirmation(s), need {need} in {tr}"
                              + (f" (chart {s.regime})" if tr != s.regime else "")
                              + (" (+1 for volatility shock)" if s.vol_shock else ""))
    limit = plan_limit(s.close, kind, cfg)
    if kind == "REBOUND_INITIAL":
        structure = s.ema[50] - 1.25 * s.atr
    elif kind in ("TREND_INITIAL", "MOMENTUM_INITIAL"):
        structure = min(s.pullback_low, s.ema[20] - 0.5 * s.atr)
    else:
        structure = s.pullback_low - 0.5 * s.atr
    if kind == "REBOUND_INITIAL" and cfg.strategy_mode == "momentum":
        stop = round(limit - prof.stop_atr_min * s.atr, 2)
    else:
        dist = limit - structure
        if dist > prof.stop_atr_max * s.atr:
            return Rejection(sym, f"stop below structure would be {dist / s.atr:.1f} ATR away "
                                  f"(> {prof.stop_atr_max}); setup too loose")
        stop = round(min(structure, limit - prof.stop_atr_min * s.atr), 2)
    if pos is not None:
        stop = round(min(stop, pos.stop), 2)
    t = targets_for(s, prof, cfg)
    rr = t.preferred * limit / (limit - stop)
    score = round(100 * (0.4 * min(rr / 2, 1.0) + 0.3 * min(n / 6, 1.0) + 0.2 * REGIME_WEIGHT.get(tr, 0.5)
                         + 0.1 * min(s.adx / 30, 1.0)), 1)
    reasons += s.confirmations
    reasons.append(f"regime {tr}" + (f" (daily label {s.regime})" if tr != s.regime else f" ({s.regime_detail})"))
    return EntrySignal(sym, 1 if pos is None else pos.tranches + 1, kind, limit, stop, t, score,
                       tr, s.pullback, reasons)


def size_core(sym: str, s: Snapshot, prof: Profile, cfg: EtfConfig, equity: float, cash: float,
              exposure: dict, pos: Optional[PositionView]) -> tuple[Optional[EntrySignal], Sizing]:
    """Top up toward the momentum core target so big up-days are captured while trend is intact."""
    frac = core_target_fraction(trading_regime(s, cfg), cfg)
    z = Sizing()
    if frac <= 0:
        z.reasons.append("no core target in this regime")
        return None, z
    slip = prof.slippage_bps / 1e4
    cap = equity * min(prof.max_exposure_pct, cfg.max_alloc_pct) / 100 * frac
    held = (pos.qty * s.close) if pos else 0.0
    gap = cap - held
    eff = s.close * (1 + slip)
    if gap < eff * 0.98:
        z.reasons.append("core allocation already at target")
        return None, z
    limit = plan_limit(s.close, "CORE_TOPUP", cfg)
    stop = round(min(s.ema[50] - 1.25 * s.atr, limit - prof.stop_atr_min * s.atr), 2)
    t = targets_for(s, prof, cfg)
    tranche = 1 if pos is None else min(pos.tranches + 1, 3)
    tr_reg = trading_regime(s, cfg)
    sig = EntrySignal(sym, tranche, "CORE_TOPUP", limit, stop, t, 70.0, tr_reg, s.pullback,
                      [f"core target {frac:.0%} of equity (~Rs{cap:,.0f}), held Rs{held:,.0f}, gap Rs{gap:,.0f}",
                       f"regime {tr_reg}" + (f" (daily {s.regime})" if tr_reg != s.regime else "")])
    z = size(sig, prof, cfg, equity, cash, exposure, pos, s.vol_shock, core_qty=math.floor(gap / eff))
    return (sig, z) if z.qty > 0 else (None, z)


def size(sig: EntrySignal, prof: Profile, cfg: EtfConfig, equity: float, cash: float,
         exposure: dict, pos: Optional[PositionView], vol_shock: bool = False,
         core_qty: Optional[int] = None) -> Sizing:
    """Quantity for this tranche. exposure: symbol -> current market value held."""
    z = Sizing()
    factor = regime_rules(cfg)[sig.regime][0] * (0.5 if vol_shock else 1.0)
    slip = prof.slippage_bps / 1e4
    eff = sig.limit * (1 + slip)
    planned = pos.planned_value if pos else equity * min(prof.max_exposure_pct, cfg.max_alloc_pct) / 100 * factor
    z.planned_value = planned
    if sig.kind == "CORE_TOPUP" and core_qty is not None:
        want = core_qty * eff
    else:
        want = planned * cfg.tranche_weights[sig.tranche - 1]
        if sig.tranche == 3 and pos is not None:
            want = max(planned - pos.qty * pos.avg, 0.0)
    held_here = exposure.get(sig.symbol, 0.0)
    held_other = sum(v for k, v in exposure.items() if k != sig.symbol)
    room = min(equity * prof.max_exposure_pct / 100 - held_here,
               equity * cfg.max_alloc_pct / 100 - held_here - held_other,
               (equity * cfg.max_combined_pct / 100 - held_here - held_other) if held_other > 0 else float("inf"))
    qty = math.floor(max(min(want, room), 0.0) / eff)
    if qty <= 0:
        z.reasons.append("no room under the exposure limits" if room < want else "tranche value below one unit")
        return z
    old_q, old_avg = (pos.qty, pos.avg) if pos else (0, 0.0)
    budget = equity * prof.max_loss_pct / 100

    def risk_at(q: int) -> tuple[float, float]:
        tot = old_q + q
        avg = (old_q * old_avg + q * eff) / tot
        loss = (avg - sig.stop * (1 - slip)) * tot + round_trip_cost(avg, sig.stop, tot, cfg.costs)
        return loss, avg

    while qty > 0 and risk_at(qty)[0] > budget:
        qty = math.floor(qty * 0.95) if qty > 20 else qty - 1
    if qty <= 0:
        z.reasons.append(f"loss at stop would exceed {prof.max_loss_pct}% of equity")
        return z
    while qty > 0 and qty * eff + order_charges(qty * eff, "BUY", cfg.costs) > cash:
        qty = math.floor(qty * 0.98) if qty > 50 else qty - 1
    if qty <= 0:
        z.reasons.append(f"not enough settled cash (Rs{cash:,.0f})")
        return z
    loss, avg = risk_at(qty)
    tot = old_q + qty
    tgt = avg * (1 + sig.targets.preferred)
    reward = (tgt * (1 - slip) - avg) * tot - round_trip_cost(avg, tgt, tot, cfg.costs)
    z.net_rr = reward / loss if loss > 0 else 0.0
    if reward <= 0:
        z.reasons.append("expected profit at target does not cover costs")
        return z
    if sig.kind != "CORE_TOPUP":
        if z.net_rr < cfg.min_net_rr:
            z.reasons.append(f"net reward/risk {z.net_rr:.2f} < {cfg.min_net_rr} after costs")
            return z
    z.qty, z.risk_amount, z.new_avg, z.new_stop = qty, loss, avg, sig.stop
    return z


def review(pos: PositionView, s: Snapshot, prof: Profile, cfg: EtfConfig) -> Review:
    """End-of-day review of an open position: trailing stop, target extension, indicator exits."""
    t = targets_for(s, prof, cfg)
    be = breakeven_frac(prof, cfg, pos.qty * pos.avg)
    pnl = s.close / pos.avg - 1
    peak = max(pos.peak_close, s.close)
    r = Review(stop=pos.stop, target=pos.target, extended=pos.extended)
    if peak >= pos.avg * (1 + t.minimum):
        trail = round(max(peak - prof.trail_atr * s.atr, pos.avg * (1 + be)), 2)
        if trail > r.stop:
            r.notes.append(f"trailing stop raised {r.stop:.2f} -> {trail:.2f}")
            r.stop = trail
    strong = s.adx >= 25 and s.hist > 0 and s.hist >= s.hist_prev and 50 <= s.rsi < 72 and s.close > s.ema[9]
    if pnl >= t.minimum and strong:
        ext = round(pos.avg * (1 + t.extended), 2)
        if ext > r.target:
            r.target, r.extended = ext, True
            r.notes.append(f"momentum strong (ADX {s.adx:.0f}, MACD rising): target extended to {ext:.2f}")
        lock = round(pos.avg * (1 + t.minimum / 2), 2)
        if lock > r.stop:
            r.stop = lock
            r.notes.append(f"profit lock stop {lock:.2f}")
    if s.regime == STRONG_BEAR or (cfg.strategy_mode == "momentum" and s.regime == MILD_BEAR):
        r.exit_reason = "TREND_CHANGE"
    elif cfg.exit_on_rsi_overbought and pnl >= t.minimum and s.rsi >= 70 and s.rsi < s.rsi_prev \
            and not (cfg.strategy_mode in ("aggressive", "momentum") and strong):
        r.exit_reason = "RSI_OVERBOUGHT"
    elif cfg.exit_on_macd_cross and pnl > be and s.macd < s.macd_signal and s.hist_prev >= 0:
        r.exit_reason = "MACD_BEARISH_CROSS"
    elif cfg.exit_on_support_lost and pnl < 0 and pos.sessions_held >= 3 and s.close < s.ema[9] and s.close < s.ema[20] and s.hist < 0 \
            and s.close < s.pullback_low + 0.25 * s.atr:
        r.exit_reason = "SUPPORT_LOST"
    elif pos.sessions_held >= prof.max_hold_days and pnl < t.minimum:
        r.exit_reason = "TIME_STOP"
    return r


def sudden_drop_level(prev_close: float, day_high: float, atr_pct: float, prof: Profile) -> tuple[float, float]:
    """(trigger from previous close, trigger from today's high). Price at/below either => sell now."""
    thr = max(prof.sudden_drop_pct / 100, prof.sudden_drop_atr * atr_pct)
    return round(prev_close * (1 - thr), 2), round(day_high * (1 - thr), 2)


def rank(signals: list[EntrySignal]) -> list[EntrySignal]:
    """Capital rotation: the better risk-adjusted opportunity gets capital first."""
    return sorted(signals, key=lambda x: x.score, reverse=True)
