"""Pure opportunity detectors — no DB access, no side effects, no alerts.

Each detector takes candle DataFrames (and optional 15m context) and returns
a list of apex.opportunity.contract.DetectorFinding. Identity assignment
(opportunity_uid/fingerprint) and persistence are the engine's job, not the
detector's — see apex.opportunity.engine.
"""
from __future__ import annotations
