"""Dataclass models mirroring DB tables."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Market:
    symbol: str
    is_active: bool = True
    is_priority: bool = False
    scan_enabled: bool = True
    last_24h_volume_usd: Optional[float] = None
    last_open_interest_usd: Optional[float] = None
    last_mid_price: Optional[float] = None
    id: Optional[int] = None


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: int   # Unix ms
    close_time: int  # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    is_closed: bool = True
    id: Optional[int] = None


@dataclass
class Alert:
    alert_uid: str
    symbol: str
    direction: str   # LONG | SHORT
    alert_type: str  # SETUP_FORMING | CONFIRMED_SETUP
    setup_type: str = "TREND_PULLBACK"
    status: str = "SENT"
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    reference_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    target_2r: Optional[float] = None
    invalidation_price: Optional[float] = None
    risk_usd: Optional[float] = None
    suggested_notional_usd: Optional[float] = None
    stop_distance_pct: Optional[float] = None
    confidence_score: Optional[float] = None
    message: Optional[str] = None
    expires_at: Optional[str] = None
    sent_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class PaperTrade:
    trade_uid: str
    alert_id: int
    symbol: str
    direction: str
    setup_type: str
    entry_price: float
    stop_price: float
    target_1r: float
    target_2r: float
    risk_usd: float
    opened_at: str
    suggested_notional_usd: Optional[float] = None
    status: str = "OPEN"
    outcome_at: Optional[str] = None
    followup_sent_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class DailyRisk:
    trade_date: str
    planned_loss_used_usd: float
    max_planned_loss_usd: float
    lockout_active: bool = False
    id: Optional[int] = None


@dataclass
class Snooze:
    symbol: str
    snoozed_until: str
    reason: Optional[str] = None
    id: Optional[int] = None


@dataclass
class SignalObservation:
    observation_uid: str
    observed_at: str
    symbol: str
    direction: str   # LONG | SHORT
    signal_type: str  # SETUP_FORMING | CONFIRMED_SETUP
    expires_at: str
    # Price levels are None for SETUP_FORMING (no risk_plan at that stage)
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    target_2r: Optional[float] = None
    status: str = "OBSERVED"
    outcome_r: Optional[float] = None
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None
    closed_at: Optional[str] = None
    metadata_json: Optional[str] = None
    # Milestone 10A: non-terminal TP1 tracking
    hit_1r_at: Optional[str] = None
    hit_2r_at: Optional[str] = None
    stopped_at: Optional[str] = None
    expired_at: Optional[str] = None
    first_terminal_status: Optional[str] = None
    final_status: Optional[str] = None
    time_to_1r_seconds: Optional[float] = None
    time_to_2r_seconds: Optional[float] = None
    time_to_stop_seconds: Optional[float] = None
    time_to_expiry_seconds: Optional[float] = None
    hit_1r_before_stop: Optional[int] = None
    hit_1r_before_expiry: Optional[int] = None
    id: Optional[int] = None


@dataclass
class SignalFeature:
    """Snapshot of indicator/market context at observation time.

    Captured once per new observation insert. Outcome fields are populated
    later by update_signal_feature_outcome_from_observation.
    """
    observation_uid: str
    captured_at: str
    symbol: str
    direction: str
    signal_type: str
    observed_at: str
    # Price levels
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    target_2r: Optional[float] = None
    # Candle context
    candle_open: Optional[float] = None
    candle_high: Optional[float] = None
    candle_low: Optional[float] = None
    candle_close: Optional[float] = None
    candle_volume: Optional[float] = None
    candle_open_time: Optional[int] = None
    # Indicators
    rsi_val: Optional[float] = None
    atr_val: Optional[float] = None
    vwap_val: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    price_vs_vwap_pct: Optional[float] = None
    ema_spread_pct: Optional[float] = None
    atr_pct: Optional[float] = None
    # Trend/setup context
    trend_bias: Optional[str] = None
    trend_reason: Optional[str] = None
    pullback_state: Optional[str] = None
    pullback_reason: Optional[str] = None
    # Market context
    btc_trend_bias: Optional[str] = None
    eth_trend_bias: Optional[str] = None
    market_regime_label: Optional[str] = None
    relative_strength_rank: Optional[int] = None
    relative_strength_score: Optional[float] = None
    # Outcome fields (synced after close)
    outcome_status: Optional[str] = None
    outcome_r: Optional[float] = None
    hit_1r_at: Optional[str] = None
    hit_2r_at: Optional[str] = None
    stopped_at: Optional[str] = None
    expired_at: Optional[str] = None
    time_to_1r_seconds: Optional[float] = None
    time_to_2r_seconds: Optional[float] = None
    time_to_stop_seconds: Optional[float] = None
    time_to_expiry_seconds: Optional[float] = None
    hit_1r_before_stop: Optional[int] = None
    hit_1r_before_expiry: Optional[int] = None
    # Metadata
    feature_version: str = "11C_v1"
    metadata_json: Optional[str] = None
    id: Optional[int] = None
