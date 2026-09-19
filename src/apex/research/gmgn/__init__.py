"""GMGN meme-coin research pilot — strictly read-only, coordinator-sealed.

This package is a standalone research library. It is not wired into
`apex.config`, `apex.scheduler`, `apex.db`, `apex.opportunity`,
`apex.strategy`, `apex.notifications`, or `apex.app`, and nothing in it may
become wired in without a separate, explicit approval. It never places
orders, sizes positions, quotes swaps, or sends alerts.

Modules:
  - `contract.py`  — versioned, immutable, provenance-carrying research
    record dataclasses.
  - `transport.py` — a small async HTTPS client fixed to
    `https://openapi.gmgn.ai`, enforcing an exact method/path allowlist
    before any I/O, with a conservative rate limiter and bounded retries.
  - `normalize.py` — builds frozen `contract.py` records from raw vendor
    mappings, sanitizing untrusted text and computing canonical response
    hashes.
  - `funnel.py`    — a pure, deterministic research-candidate funnel
    (`REJECTED` / `WATCH` / `ELIGIBLE_FOR_REVIEW`) with no trade, alert, or
    order output of any kind.
"""
from __future__ import annotations
