#!/usr/bin/env python3
"""Smoke test: fetch Hyperliquid perp market data and print discovered symbols."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apex.config import get_settings
from apex.data.hyperliquid_client import HyperliquidClient


async def main():
    settings = get_settings()
    client = HyperliquidClient(settings.hyperliquid_info_url)

    print(f"Connecting to: {settings.hyperliquid_info_url}")
    print()

    # 1. Fetch perp metadata
    print("--- Fetching perp metadata (type=meta) ---")
    meta = await client.get_perp_meta()
    if not meta:
        print("FAILED: Could not fetch meta")
        sys.exit(1)

    universe = meta.get("universe", [])
    print(f"Discovered {len(universe)} perp symbols:")
    symbols = [a.get("name", "?") for a in universe[:20]]
    print("  " + ", ".join(symbols))
    if len(universe) > 20:
        print(f"  ... and {len(universe) - 20} more")
    print()

    # 2. Fetch all mids
    print("--- Fetching all mids (type=allMids) ---")
    mids = await client.get_all_mids()
    if not mids:
        print("FAILED: Could not fetch mids")
        sys.exit(1)

    print(f"Got mid prices for {len(mids)} symbols")
    for sym in ["BTC", "ETH", "SOL"]:
        if sym in mids:
            print(f"  {sym}: ${float(mids[sym]):,.2f}")
    print()

    # 3. Fetch metaAndAssetCtxs
    print("--- Fetching metaAndAssetCtxs ---")
    ctx = await client.get_meta_and_asset_ctxs()
    if ctx:
        asset_ctxs = ctx[1]
        print(f"Got asset context for {len(asset_ctxs)} symbols")
        meta2 = ctx[0]
        for i, asset in enumerate(meta2.get("universe", [])[:5]):
            sym = asset.get("name", "?")
            c = asset_ctxs[i] if i < len(asset_ctxs) else {}
            vol = c.get("dayNtlVlm", "N/A")
            oi = c.get("openInterest", "N/A")
            print(f"  {sym}: vol=${vol}, OI={oi}")
    else:
        print("NOTE: metaAndAssetCtxs not available (may not be a public endpoint)")
        print("      Volume filtering will fall back to priority-symbols-only mode.")
    print()

    # 4. Candle snapshot for BTC
    print("--- Fetching BTC 5m candle snapshot ---")
    import time
    end = int(time.time() * 1000)
    start = end - 3 * 60 * 60 * 1000  # last 3h
    candles = await client.get_candle_snapshot("BTC", "5m", start, end)
    if candles:
        print(f"Got {len(candles)} candles for BTC/5m")
        last = candles[-1]
        print(f"  Last candle: open={last.get('o')}, high={last.get('h')}, "
              f"low={last.get('l')}, close={last.get('c')}, vol={last.get('v')}")
    else:
        print("NOTE: candleSnapshot returned no data or is not available")
        print("      Check Hyperliquid API docs for correct candleSnapshot payload.")
    print()

    print("Smoke test complete.")


if __name__ == "__main__":
    asyncio.run(main())
