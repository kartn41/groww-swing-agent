"""Strategy registry and the explicit regime rules (spec section 6: which strategies may run when)."""
from app.strategies import breakout, pullback, rs_continuation, vcp

STRATEGIES = {
    "breakout": breakout.evaluate,
    "pullback": pullback.evaluate,
    "rs_continuation": rs_continuation.evaluate,
    "vcp": vcp.evaluate,
}

# No new long entries in SIDEWAYS, WEAK_DOWN, STRONG_DOWN, HIGH_VOLATILITY or UNKNOWN.
REGIME_STRATEGIES = {
    "STRONG_UP": ["breakout", "pullback", "rs_continuation", "vcp"],
    "WEAK_UP": ["pullback", "vcp"],          # breakouts fail more often without broad participation
}


def permitted(regime_name: str, allowed: list[str] | None = None) -> list[str]:
    names = REGIME_STRATEGIES.get(regime_name, [])
    return [n for n in names if allowed is None or n in allowed]
