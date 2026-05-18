"""Position sizing and risk calculations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskPlan:
    direction: str
    entry_price: float
    stop_price: float
    target_1r: float
    target_2r: float
    risk_usd: float
    suggested_notional_usd: float
    stop_distance_pct: float
    r_distance: float


def calculate_risk_plan(
    direction: str,
    entry_price: float,
    stop_price: float,
    account_size_usd: float,
    risk_per_trade_pct: float,
    max_stop_distance_pct: float,
) -> Optional[RiskPlan]:
    """Return a RiskPlan or None if setup is invalid."""
    if entry_price <= 0 or stop_price <= 0:
        return None

    if direction == "LONG":
        if stop_price >= entry_price:
            return None
        r = entry_price - stop_price
        target_1r = entry_price + r
        target_2r = entry_price + 2 * r
    elif direction == "SHORT":
        if stop_price <= entry_price:
            return None
        r = stop_price - entry_price
        target_1r = entry_price - r
        target_2r = entry_price - 2 * r
    else:
        return None

    stop_distance_pct = abs(entry_price - stop_price) / entry_price * 100
    if stop_distance_pct > max_stop_distance_pct:
        return None

    risk_usd = account_size_usd * risk_per_trade_pct / 100
    if stop_distance_pct == 0:
        return None

    suggested_notional = risk_usd / (stop_distance_pct / 100)

    return RiskPlan(
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_1r=target_1r,
        target_2r=target_2r,
        risk_usd=risk_usd,
        suggested_notional_usd=suggested_notional,
        stop_distance_pct=stop_distance_pct,
        r_distance=r,
    )
