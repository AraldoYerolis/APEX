"""In-memory candle store with SQLite persistence."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from apex.db import repository as repo
from apex.db.models import Candle

logger = logging.getLogger(__name__)

# Max candles kept per (symbol, timeframe) in memory
MAX_CANDLES_IN_MEMORY = 300

# Max age of latest candle before data is considered stale (seconds)
STALE_THRESHOLD_SECONDS: dict[str, int] = {
    "1m": 90,
    "3m": 200,
    "5m": 360,
    "15m": 1000,
}


@dataclass
class CandleKey:
    symbol: str
    timeframe: str


class CandleStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # {(symbol, timeframe): deque of dicts sorted oldest-first}
        self._data: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._last_update: dict[tuple[str, str], float] = {}

    def update(self, symbol: str, timeframe: str, candle: dict, persist: bool = True) -> None:
        """Insert or update a candle. candle dict must have: t, T, o, h, l, c, v, closed."""
        key = (symbol, timeframe)
        store = self._data[key]

        open_time = int(candle.get("t", candle.get("open_time", 0)))

        # Find and replace if same open_time exists
        for i, c in enumerate(store):
            if c["open_time"] == open_time:
                store[i] = _normalize(symbol, timeframe, candle)
                break
        else:
            store.append(_normalize(symbol, timeframe, candle))
            store.sort(key=lambda x: x["open_time"])

        # Trim to max size
        if len(store) > MAX_CANDLES_IN_MEMORY:
            self._data[key] = store[-MAX_CANDLES_IN_MEMORY:]

        self._last_update[key] = time.time()

        # Persist closed candles to SQLite
        if persist and candle.get("closed", candle.get("is_closed", True)):
            try:
                c_obj = Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=int(candle.get("T", candle.get("close_time", open_time))),
                    open=float(candle.get("o", candle.get("open", 0))),
                    high=float(candle.get("h", candle.get("high", 0))),
                    low=float(candle.get("l", candle.get("low", 0))),
                    close=float(candle.get("c", candle.get("close", 0))),
                    volume=float(candle.get("v", candle.get("volume", 0))),
                    is_closed=True,
                )
                repo.upsert_candle(self._conn, c_obj)
            except Exception as e:
                logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")

    def load_from_db(self, symbol: str, timeframe: str, limit: int = 200) -> None:
        """Preload candles from SQLite into memory."""
        rows = repo.get_candles(self._conn, symbol, timeframe, limit=limit)
        for row in reversed(rows):  # rows come newest-first; reverse to oldest-first
            self._data[(symbol, timeframe)].append({
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "open_time": row["open_time"],
                "close_time": row["close_time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "is_closed": bool(row["is_closed"]),
            })

    def get_df(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Return a DataFrame of closed candles (oldest first), or None if empty."""
        key = (symbol, timeframe)
        candles = [c for c in self._data[key] if c.get("is_closed", True)]
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        required = ["open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                return None
        return df[["open_time", "open", "high", "low", "close", "volume"]].copy()

    def is_stale(self, symbol: str, timeframe: str) -> bool:
        key = (symbol, timeframe)
        last = self._last_update.get(key)
        if last is None:
            return True
        threshold = STALE_THRESHOLD_SECONDS.get(timeframe, 400)
        return (time.time() - last) > threshold

    def candle_count(self, symbol: str, timeframe: str) -> int:
        return len(self._data.get((symbol, timeframe), []))


def _normalize(symbol: str, timeframe: str, c: dict) -> dict:
    """Normalize raw WS candle dict to a consistent internal format."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": int(c.get("t", c.get("open_time", 0))),
        "close_time": int(c.get("T", c.get("close_time", 0))),
        "open": float(c.get("o", c.get("open", 0))),
        "high": float(c.get("h", c.get("high", 0))),
        "low": float(c.get("l", c.get("low", 0))),
        "close": float(c.get("c", c.get("close", 0))),
        "volume": float(c.get("v", c.get("volume", 0))),
        "is_closed": bool(c.get("closed", c.get("is_closed", True))),
    }
