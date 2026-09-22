"""APEX configuration via pydantic-settings."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from apex.research.gmgn.contract import SUPPORTED_CHAINS as _GMGN_RESEARCH_SUPPORTED_CHAINS

# At most this many comma-separated `chain:contract_address` identities are
# accepted in gmgn_research_watchlist — see _parse_gmgn_research_watchlist.
GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES = 10


@dataclass(frozen=True)
class GmgnWatchlistIdentity:
    """One validated `chain:contract_address` identity parsed from
    `Settings.gmgn_research_watchlist`. A ticker/symbol is never identity —
    only `chain` + `contract_address` (see apex.research.gmgn.normalize)."""

    chain: str
    contract_address: str


def _parse_gmgn_research_watchlist(raw: str) -> tuple[GmgnWatchlistIdentity, ...]:
    """Parses `gmgn_research_watchlist` into at most
    GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES validated, deduplicated (exact
    identity, first-seen order) identities.

    Fails closed to an empty tuple — never a partial/best-effort result — on
    any malformed segment (not exactly one `:`), any empty or
    whitespace-only comma-separated segment (leading, trailing, or interior,
    e.g. doubled commas), empty chain/address, an unsupported chain, an
    empty watchlist, or more than GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES
    comma-separated segments. Address case is preserved exactly (only
    surrounding whitespace is trimmed).
    """
    if not raw or not raw.strip():
        return ()
    raw_segments = raw.split(",")
    if len(raw_segments) > GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES:
        return ()
    segments: list[str] = []
    for raw_segment in raw_segments:
        segment = raw_segment.strip()
        if not segment:
            return ()
        segments.append(segment)
    seen: dict[tuple[str, str], None] = {}
    for segment in segments:
        # Exactly one colon required — "chain:contract_address" only, never
        # "chain:addr:extra" (no known supported address format uses a
        # colon, so treating extra colons as malformed is the fail-closed
        # choice rather than guessing which part is the real address).
        parts = segment.split(":")
        if len(parts) != 2:
            return ()
        chain, address = parts[0].strip(), parts[1].strip()
        if not chain or not address:
            return ()
        if chain not in _GMGN_RESEARCH_SUPPORTED_CHAINS:
            return ()
        seen.setdefault((chain, address), None)
    return tuple(GmgnWatchlistIdentity(chain=c, contract_address=a) for c, a in seen)


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

    # Trade plans and outcome evidence v0.1 (research-only, additive — see
    # src/apex/opportunity/trade_plan.py and trade_plan_outcome.py). Must be
    # explicitly enabled AND requires opportunity_engine_enabled=true AND
    # dry_run_mode=true; never influences detector eligibility/ranking,
    # scoring, alerting, sizing, or trade execution. See CLAUDE.md.
    trade_plan_evidence_enabled: bool = False

    # Live Opportunity Board v0.1 (research-only, additive — see
    # src/apex/opportunity/board.py). Local, development-only read-only UI/API
    # over existing ranked opportunities, trade plans, and outcome evidence.
    # Must be explicitly enabled AND apex_env=="development" (create_app never
    # mounts the board otherwise, in production or anywhere else); it never
    # writes anything, sizes a position, or calls alerting/trading code.
    live_opportunity_board_enabled: bool = False

    # Candle-feed diagnostics (bounded, local, default-off — see
    # src/apex/data/candle_store.py and src/apex/data/reconnecting_ws.py).
    # Adds in-memory counters and a periodic local log snapshot only when
    # explicitly enabled; never changes candle eligibility, persistence
    # decisions, subscriptions, or detector inputs.
    candle_diagnostics_enabled: bool = False

    # Closed-candle reconciliation (bounded, local, default-off — see
    # src/apex/data/candle_reconciler.py and the reconciliation section of
    # src/apex/data/candle_store.py). Fetches fresh authoritative REST data
    # only for bars a live WS rollover has proven are missing; never
    # promotes a cached forming candle to closed based on elapsed time. All
    # other tuning (tick interval, fetch/bar caps, retry/backoff) is a
    # fixed, code-reviewed default in CandleReconciler — not runtime
    # configuration — per the approved scope for this flag.
    candle_reconciliation_enabled: bool = False

    # Shadow Alert Runtime Wiring v0.1 (research-only, additive, default-off
    # — see src/apex/opportunity/shadow_runtime.py). The runtime re-checks
    # every one of these, plus opportunity_engine_enabled,
    # trade_plan_evidence_enabled, dry_run_mode, and alerts_enabled, at its
    # own entry point (defense in depth) before any candidate/cohort/
    # history/database read — see shadow_runtime.py and main.py's scheduler
    # wiring. Never sends anything, never mutates configuration to stop
    # itself, never writes to alerts/paper_trades/daily_risk/signal tables/
    # opportunity rows/plans/outcomes, and never reaches a broker/exchange,
    # notification transport, or order/trade path.
    shadow_alert_pilot_enabled: bool = False
    # Free-text pilot identity; must be non-empty for the runtime to ever
    # run (see shadow_runtime.resolve_pilot_state).
    shadow_alert_pilot_id: str = ""
    # ISO8601 UTC ("%Y-%m-%dT%H:%M:%SZ"), aware, start < deadline, spanning
    # at most 24 hours. Missing/malformed/non-UTC/nonfinite/negative/
    # reversed/expired/more-than-24-hours-apart settings fail the pilot
    # closed without evaluating any candidate — see
    # shadow_runtime.resolve_pilot_state.
    shadow_alert_pilot_start_at: str = ""
    shadow_alert_pilot_deadline_at: str = ""
    # Explicit, nonnegative round-trip cost assumptions in R. fee_r +
    # slippage_r is the one total cost both the exact cohort report and the
    # shadow-alert evaluator use — see shadow_runtime.py.
    shadow_alert_pilot_fee_r: float = 0.0
    shadow_alert_pilot_slippage_r: float = 0.0

    # GMGN read-only research runtime seam v0.1 (research-only, additive —
    # see src/apex/research/gmgn/runtime.py). Default-off. When true, main.py
    # registers exactly one additional scheduler job that calls
    # run_gmgn_research_scan with settings only — that wiring deliberately
    # never injects a real transport for this milestone, so the job always
    # takes the enabled-with-no-transport fail-closed path (see
    # runtime.run_gmgn_research_scan). The runtime itself re-checks this flag
    # at entry (defense in depth) and never performs I/O, touches an
    # injected transport, or parses the watchlist when disabled. Never
    # imports apex.notifications/apex.strategy/apex.actions, writes to a
    # database, or reaches a broker/exchange.
    gmgn_research_enabled: bool = False
    # Comma-separated `chain:contract_address` identities (at most
    # GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES), validated and deduplicated by
    # gmgn_research_watchlist_identities below. A ticker/symbol is never
    # identity — only chain+contract_address.
    gmgn_research_watchlist: str = ""

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
    def gmgn_research_watchlist_identities(self) -> tuple[GmgnWatchlistIdentity, ...]:
        return _parse_gmgn_research_watchlist(self.gmgn_research_watchlist)

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
