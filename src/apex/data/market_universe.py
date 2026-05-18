"""Market universe management — discover, filter, and persist scan-enabled perps."""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from apex.config import Settings
from apex.data.hyperliquid_client import HyperliquidClient
from apex.db import repository as repo
from apex.db.models import Market

logger = logging.getLogger(__name__)


async def refresh_universe(
    conn: sqlite3.Connection,
    client: HyperliquidClient,
    settings: Settings,
) -> list[str]:
    """Refresh market universe from Hyperliquid. Returns list of scan-enabled symbols."""

    # Try metaAndAssetCtxs first (includes volume/OI data)
    meta_ctx = await client.get_meta_and_asset_ctxs()
    symbol_volumes: dict[str, Optional[float]] = {}

    if meta_ctx:
        try:
            meta = meta_ctx[0]
            asset_ctxs = meta_ctx[1]
            universe = meta.get("universe", [])
            for i, asset in enumerate(universe):
                sym = asset.get("name", "").upper()
                if not sym:
                    continue
                ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                # dayNtlVlm is approximate 24h notional volume
                vol = ctx.get("dayNtlVlm")
                symbol_volumes[sym] = float(vol) if vol else None
            logger.info(f"Fetched {len(symbol_volumes)} symbols from metaAndAssetCtxs")
        except Exception as e:
            logger.warning(f"Failed to parse metaAndAssetCtxs: {e}")
            symbol_volumes = {}

    # Fallback: just fetch meta symbols with no volume data
    if not symbol_volumes:
        logger.info("Falling back to meta-only universe fetch (no volume data)")
        meta = await client.get_perp_meta()
        if not meta:
            logger.error("Failed to fetch perp meta — cannot refresh universe")
            return []
        for asset in meta.get("universe", []):
            sym = asset.get("name", "").upper()
            if sym:
                symbol_volumes[sym] = None  # TODO: fetch volume separately

    all_symbols = list(symbol_volumes.keys())
    logger.info(f"Discovered {len(all_symbols)} perp symbols from Hyperliquid")

    # Apply filters
    priority = set(settings.priority_symbols_list)
    excluded = set(settings.excluded_symbols_list)

    scan_symbols: list[str] = []

    for sym in all_symbols:
        if sym in excluded:
            continue
        if sym in priority:
            scan_symbols.append(sym)
            continue
        if settings.scan_mode == "MAJOR_ONLY":
            vol = symbol_volumes.get(sym)
            if vol is None:
                # TODO: no volume data available — include priority symbols only
                # until proper volume endpoint is available
                continue
            if vol >= settings.min_24h_volume_usd:
                scan_symbols.append(sym)
        else:  # ALL_PERPS
            scan_symbols.append(sym)

    # Sort by volume desc, priority symbols always included
    def sort_key(sym: str) -> float:
        if sym in priority:
            return float("inf")
        return symbol_volumes.get(sym) or 0.0

    scan_symbols.sort(key=sort_key, reverse=True)
    scan_symbols = scan_symbols[: settings.max_symbols]

    # Persist to DB
    for sym in all_symbols:
        try:
            market = Market(
                symbol=sym,
                is_active=True,
                is_priority=sym in priority,
                scan_enabled=sym in scan_symbols,
                last_24h_volume_usd=symbol_volumes.get(sym),
            )
            repo.upsert_market(conn, market)
        except Exception as e:
            logger.warning(f"Failed to upsert market {sym}: {e}")

    logger.info(
        f"Universe refreshed: {len(scan_symbols)} scan-enabled symbols "
        f"({settings.scan_mode})"
    )
    repo.log_event(
        conn,
        "UNIVERSE_REFRESH",
        f"Refreshed universe: {len(scan_symbols)} scan-enabled of {len(all_symbols)} total",
        metadata={"scan_symbols": scan_symbols[:20]},
    )

    return scan_symbols
