"""TA Opportunity Engine — additive, research-only opportunity detection.

Milestone v0.1: broad detection of short-term TA setups (VOLATILITY_COMPRESSION,
SWEEP_RECLAIM) on 3m/5m candles, recorded to opportunity_observations for later
review. Nothing in this package reads from or writes to the existing signal/
alert path (strategy/, notifications/, the alerts/paper_trades/daily_risk
tables, or Pushover). See CLAUDE.md and .claude/rules/production-safety.md.
"""
from __future__ import annotations
