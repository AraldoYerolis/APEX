"""Time utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_from_iso(s: str) -> datetime:
    """Parse ISO UTC string (with or without trailing Z)."""
    s = s.rstrip("Z")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def minutes_from_now(minutes: int) -> str:
    return (utcnow() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_ago_iso(minutes: int) -> str:
    return (utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago_iso(hours: int) -> str:
    return (utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
