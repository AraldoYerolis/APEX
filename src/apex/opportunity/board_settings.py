"""Standalone settings for the Protected Live Opportunity Board process.

This is deliberately independent of `apex.config.Settings` and every other
main-process settings/module. It must never import `apex.config`,
`apex.db.connection`, `apex.app`, or any other main-application module — see
`docs/LIVE_OPPORTUNITY_BOARD_ACCESS.md` for the full architecture this is one
piece of. All configuration is read exclusively from `APEX_BOARD_`-prefixed
environment variables; the main `.env` file is never read (`env_file=None`).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class BoardSettings(BaseSettings):
    """Board-only settings. `enabled` defaults to false (fail-closed); every
    other field is required so an incomplete environment fails at
    construction time rather than falling back to a guessed default.
    """

    model_config = SettingsConfigDict(
        env_prefix="APEX_BOARD_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    enabled: bool = False
    db_path: Path
    socket_path: Path
    allowed_identity: str
    log_level: str = "INFO"

    @field_validator("db_path", "socket_path")
    @classmethod
    def _require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("allowed_identity")
    @classmethod
    def _require_nonempty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("allowed_identity must be a nonempty string")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in SUPPORTED_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(SUPPORTED_LOG_LEVELS)}")
        return upper
