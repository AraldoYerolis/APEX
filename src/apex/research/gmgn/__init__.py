"""GMGN meme-coin research pilot — strictly read-only, coordinator-sealed.

This package has exactly one wired-in seam: `apex.config.Settings.
gmgn_research_enabled` (default off) plus `gmgn_research_watchlist`, and a
single additive scheduler job in `apex.main` (registered only when that
flag is true) that calls `runtime.run_gmgn_research_scan` with settings
only — that wiring deliberately never injects a real transport, so the job
always takes the enabled-with-no-transport fail-closed path for this
milestone; any real vendor transport requires separate, explicit wiring
authorization. Nothing here is wired into `apex.db` (persistence/schema/
migration), `apex.opportunity`, `apex.strategy`, `apex.notifications`,
broker/exchange adapters, or `apex.app`. It never places orders, sizes
positions, quotes swaps, or sends alerts.

Modules:
  - `contract.py`  — versioned, immutable, provenance-carrying research
    record dataclasses.
  - `transport.py` — a small async HTTPS client fixed to
    `https://openapi.gmgn.ai`, enforcing an exact method/path allowlist
    before any I/O, with a conservative rate limiter and bounded retries.
    Never constructed by `runtime.py`.
  - `normalize.py` — builds frozen `contract.py` records from raw vendor
    mappings, sanitizing untrusted text and computing canonical response
    hashes.
  - `funnel.py`    — a pure, deterministic research-candidate funnel
    (`REJECTED` / `WATCH` / `ELIGIBLE_FOR_REVIEW`) with no trade, alert, or
    order output of any kind.
  - `runtime.py`   — the default-off runtime seam described above:
    `run_gmgn_research_scan` receives an injected, transport-like object
    (never constructs `transport.GmgnResearchTransport` itself), calls the
    security/pool_info/holder-structure/candle allowlisted GET endpoints
    per configured watchlist identity, and feeds responses through
    `normalize.py` and `funnel.py`. Discovery/rank/search/smart-money
    endpoints are excluded from this v0 integration.
"""
from __future__ import annotations
