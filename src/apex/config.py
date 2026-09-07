"""APEX configuration via pydantic-settings."""
from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Runtime
    apex_env: Literal["development", "production"] = "development"
    apex_log_level: str = "INFO"
    # Level for the third-party `websockets` logger. WARNING matches historical
    # behaviour; raise to DEBUG only while diagnosing feed drops.
    apex_ws_log_level: str = "WARNING"
    apex_db_path: str = "./data/apex.db"
    apex_public_base_url: str = ""

    # Alert controls
    alerts_enabled: bool = False
    dry_run_mode: bool = True
    # Comma-separated: SETUP_FORMING, CONFIRMED_SETUP, or both
    alert_types_enabled: str = "CONFIRMED_SETUP"

    # TA Opportunity Engine (research-only, additive — see src/apex/opportunity/).
    # Must be explicitly enabled AND requires dry_run_mode=true; never routes
    # to signal generation, alerting, or trade execution. See CLAUDE.md.
    opportunity_engine_enabled: bool = False

    # FastAPI
    apex_host: str = "127.0.0.1"
    apex_port: int = 8000

    # Hyperliquid
    hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"

    # Market universe
    scan_mode: Literal["MAJOR_ONLY", "ALL_PERPS"] = "MAJOR_ONLY"
    min_24h_volume_usd: float = 25_000_000
    priority_symbols: str = "BTC,ETH,SOL,HYPE"
    excluded_symbols: str = ""
    max_symbols: int = 30

    # Strategy timeframes
    trend_timeframe: str = "15m"
    setup_timeframe: str = "5m"
    entry_timeframe: str = "3m"

    # Indicators
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    vwap_lookback_candles: int = 96
    stop_atr_multiplier: float = 1.25

    # Risk
    account_size_usd: float = 100.0
    risk_per_trade_pct: float = 1.0
    max_daily_planned_loss_pct: float = 3.0
    max_stop_distance_pct: float = 4.0

    # Alert throttles
    confirmed_alert_cooldown_minutes: int = 30
    forming_alert_cooldown_minutes: int = 15
    global_confirmed_alerts_per_hour: int = 5
    setup_expiration_minutes: int = 15
    snooze_minutes: int = 15

    # Paper trade tracking
    followup_after_enter_minutes: int = 30

    # Pushover
    pushover_app_token: str = ""
    pushover_user_key: str = ""
    pushover_device: str = ""
    pushover_default_priority: int = 0
    pushover_confirmed_priority: int = 1
    pushover_sound: str = "pushover"

    # Action link security
    action_token_secret: str = "change-me-to-a-random-secret"
    action_token_ttl_hours: int = 48

    # --- derived helpers ---

    @property
    def alert_types_enabled_list(self) -> list[str]:
        valid = {"SETUP_FORMING", "CONFIRMED_SETUP"}
        result = [t.strip().upper() for t in self.alert_types_enabled.split(",") if t.strip()]
        return [t for t in result if t in valid]

    @property
    def priority_symbols_list(self) -> list[str]:
        return [s.strip().upper() for s in self.priority_symbols.split(",") if s.strip()]

    @property
    def excluded_symbols_list(self) -> list[str]:
        return [s.strip().upper() for s in self.excluded_symbols.split(",") if s.strip()]

    @property
    def max_risk_usd(self) -> float:
        return self.account_size_usd * self.risk_per_trade_pct / 100

    @property
    def max_daily_loss_usd(self) -> float:
        return self.account_size_usd * self.max_daily_planned_loss_pct / 100

    @field_validator("apex_log_level", "apex_ws_log_level")
    @classmethod
    def _validate_log_level(cls, v: str, info: ValidationInfo) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"{info.field_name} must be one of {valid}")
        return v.upper()

    @model_validator(mode="after")
    def _startup_warnings(self) -> "Settings":
        missing_pushover = []
        if not self.pushover_app_token:
            missing_pushover.append("PUSHOVER_APP_TOKEN")
        if not self.pushover_user_key:
            missing_pushover.append("PUSHOVER_USER_KEY")
        if missing_pushover:
            print(
                f"[APEX CONFIG WARNING] Pushover not configured: {missing_pushover}. "
                "Alerts will not be sent.",
                file=sys.stderr,
            )
        if not self.apex_public_base_url:
            print(
                "[APEX CONFIG WARNING] APEX_PUBLIC_BASE_URL is not set. "
                "Action links will be disabled in alerts.",
                file=sys.stderr,
            )
        if self.action_token_secret == "change-me-to-a-random-secret":
            print(
                "[APEX CONFIG WARNING] ACTION_TOKEN_SECRET is using the default value. "
                "Set a strong random secret in production.",
                file=sys.stderr,
            )
        if not self.alerts_enabled:
            print(
                "[APEX CONFIG] ALERTS_ENABLED=false — strategy alerts will be logged but NOT sent.",
                file=sys.stderr,
            )
        if self.dry_run_mode:
            print(
                "[APEX CONFIG] DRY_RUN_MODE=true — logs will be prefixed [DRY RUN].",
                file=sys.stderr,
            )
        if not self.alert_types_enabled_list:
            print(
                "[APEX CONFIG WARNING] ALERT_TYPES_ENABLED is empty or invalid. "
                "No alert types will be sent.",
                file=sys.stderr,
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
