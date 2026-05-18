"""Hyperliquid public API client."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self, info_url: str = "https://api.hyperliquid.xyz/info") -> None:
        self.info_url = info_url

    async def _post(self, payload: dict, retries: int = 3) -> Optional[Any]:
        """POST to info endpoint with exponential backoff."""
        import asyncio

        delay = 1.0
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(self.info_url, json=payload)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning(
                    f"Hyperliquid HTTP {e.response.status_code} (attempt {attempt+1}): {e}"
                )
            except httpx.TransportError as e:
                logger.warning(f"Hyperliquid transport error (attempt {attempt+1}): {e}")
            except Exception as e:
                logger.error(f"Hyperliquid unexpected error (attempt {attempt+1}): {e}")

            if attempt < retries - 1:
                await asyncio.sleep(delay)
                delay *= 2

        logger.error(f"Hyperliquid request failed after {retries} attempts")
        return None

    async def get_perp_meta(self) -> Optional[dict]:
        """Fetch perpetual market metadata.

        Returns dict with 'universe' key listing all perp assets.
        """
        result = await self._post({"type": "meta"})
        if result is None:
            return None
        if not isinstance(result, dict) or "universe" not in result:
            logger.error(f"Unexpected meta response structure: {type(result)}")
            return None
        return result

    async def get_all_mids(self) -> Optional[dict[str, str]]:
        """Fetch all mid prices. Returns {symbol: mid_price_str}."""
        result = await self._post({"type": "allMids"})
        if result is None:
            return None
        if not isinstance(result, dict):
            logger.error(f"Unexpected allMids response: {type(result)}")
            return None
        return result

    async def get_candle_snapshot(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: Optional[int] = None,
    ) -> Optional[list[dict]]:
        """Fetch historical candle snapshot.

        interval: '1m', '3m', '5m', '15m', etc.
        start_time / end_time: Unix milliseconds.

        Returns list of candle dicts.

        TODO: Verify exact request format against Hyperliquid docs.
        The candleSnapshot endpoint structure may differ from the info endpoint.
        Adjust payload keys if API returns errors.
        """
        import time

        payload: dict = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": start_time,
            },
        }
        if end_time is not None:
            payload["req"]["endTime"] = end_time

        result = await self._post(payload)
        if result is None:
            return None
        if not isinstance(result, list):
            logger.warning(
                f"Unexpected candleSnapshot response for {symbol}/{interval}: {type(result)}"
            )
            return None
        return result

    async def get_meta_and_asset_ctxs(self) -> Optional[list]:
        """Fetch meta + asset contexts (includes open interest, funding, etc).

        Returns [meta_dict, [asset_ctx, ...]] or None.

        TODO: Confirm this endpoint is available on mainnet public API.
        The 'metaAndAssetCtxs' type may require authentication or may not exist.
        Fall back gracefully if unavailable.
        """
        result = await self._post({"type": "metaAndAssetCtxs"})
        if result is None:
            return None
        if not isinstance(result, list) or len(result) < 2:
            logger.warning(f"Unexpected metaAndAssetCtxs structure")
            return None
        return result
