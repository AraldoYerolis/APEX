"""Regression tests for the Hyperliquid canonical-symbol casing fix.

Root cause: APEX's internal symbol identity is uppercase (BTC, KPEPE, ...),
but Hyperliquid's own asset names are not all uppercase — e.g. the 1000x
rebase class uses a lowercase "k" prefix ("kPEPE"). Sending the uppercased
internal identity on an outbound WebSocket subscribe or REST candleSnapshot
request produces an asset name Hyperliquid doesn't recognize; an isolated
production test proved this causes an abnormal socket close in ~0.5s.

These tests lock in:
- the upstream (canonical) name is captured before uppercasing, on both the
  metaAndAssetCtxs and meta-only-fallback universe paths
- internal identity (scan_symbols, DB rows, CandleStore keys) stays uppercase
- outbound WS subscriptions and outbound REST backfill requests use the
  upstream spelling
- inbound WS candle messages still normalize to uppercase before storage
- a symbol with no known mapping falls back to itself (today's behavior)
- the fix is generic, not PEPE-specific (a synthetic mixed-case asset proves it)
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apex import main
from apex.config import Settings
from apex.data import market_universe
from apex.data.candle_store import CandleStore
from apex.data.market_universe import get_upstream_symbol, refresh_universe
from apex.db.connection import init_db
from apex.main import backfill_candles, build_ws_subscriptions, on_ws_message


@pytest.fixture(autouse=True)
def _reset_upstream_map():
    """The upstream map is process-global; isolate every test."""
    market_universe._reset_upstream_symbol_map()
    yield
    market_universe._reset_upstream_symbol_map()


def _settings(**overrides) -> Settings:
    defaults = dict(
        scan_mode="MAJOR_ONLY",
        min_24h_volume_usd=0,
        priority_symbols="",
        excluded_symbols="",
        max_symbols=30,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _meta_and_asset_ctxs(names: list[str]) -> list:
    universe = [{"name": n} for n in names]
    asset_ctxs = [{"dayNtlVlm": "1000000"} for _ in names]
    return [{"universe": universe}, asset_ctxs]


# --------------------------------------------------------- metaAndAssetCtxs

class TestMetaAndAssetCtxsPath:
    @pytest.mark.asyncio
    async def test_kpepe_upstream_mapping_captured(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = _meta_and_asset_ctxs(
            ["BTC", "kPEPE", "TRUMP"]
        )

        scan_symbols = await refresh_universe(conn, client, _settings())

        assert "KPEPE" in scan_symbols
        assert get_upstream_symbol("KPEPE") == "kPEPE"

    @pytest.mark.asyncio
    async def test_internal_identity_stays_uppercase(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = _meta_and_asset_ctxs(
            ["BTC", "kPEPE", "TRUMP"]
        )

        scan_symbols = await refresh_universe(conn, client, _settings())

        assert set(scan_symbols) == {"BTC", "KPEPE", "TRUMP"}
        assert all(s == s.upper() for s in scan_symbols)


# ------------------------------------------------------- meta-only fallback

class TestMetaOnlyFallbackPath:
    @pytest.mark.asyncio
    async def test_kpepe_upstream_mapping_captured_on_fallback(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = None  # force fallback
        client.get_perp_meta.return_value = {
            "universe": [{"name": "BTC"}, {"name": "kPEPE"}, {"name": "TRUMP"}]
        }

        await refresh_universe(conn, client, _settings())

        assert get_upstream_symbol("KPEPE") == "kPEPE"
        assert get_upstream_symbol("BTC") == "BTC"
        assert get_upstream_symbol("TRUMP") == "TRUMP"


# ------------------------------------------------------------- BTC / TRUMP

class TestUnaffectedSymbols:
    @pytest.mark.asyncio
    async def test_btc_internal_and_outbound_both_btc(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = _meta_and_asset_ctxs(["BTC"])
        await refresh_universe(conn, client, _settings())

        assert get_upstream_symbol("BTC") == "BTC"

    @pytest.mark.asyncio
    async def test_trump_internal_and_outbound_both_trump(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = _meta_and_asset_ctxs(["TRUMP"])
        await refresh_universe(conn, client, _settings())

        assert get_upstream_symbol("TRUMP") == "TRUMP"


# ------------------------------------------------------------------- WS out

class TestWebSocketOutboundBoundary:
    def test_kpepe_sends_lowercase_k_coin_never_uppercase(self):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")

        subs = build_ws_subscriptions(["BTC", "KPEPE", "TRUMP"], ["1m"])

        coins = {s["coin"] for s in subs}
        assert "kPEPE" in coins
        assert "KPEPE" not in coins

    def test_subscription_count_order_and_timeframes_unchanged(self):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        symbols = ["BTC", "ETH", "KPEPE", "TRUMP"]
        timeframes = ["15m", "5m", "3m", "1m"]

        subs = build_ws_subscriptions(symbols, timeframes)

        assert len(subs) == len(symbols) * len(timeframes)
        # symbol-major order preserved; only the KPEPE group's coin changes.
        expected_coin_by_symbol = {
            "BTC": "BTC", "ETH": "ETH", "KPEPE": "kPEPE", "TRUMP": "TRUMP",
        }
        idx = 0
        for symbol in symbols:
            for tf in timeframes:
                sub = subs[idx]
                assert sub["type"] == "candle"
                assert sub["coin"] == expected_coin_by_symbol[symbol]
                assert sub["interval"] == tf
                idx += 1

    def test_no_mapping_falls_back_to_internal_symbol(self):
        # No refresh_universe call, no _record_upstream_symbol call: map is empty.
        subs = build_ws_subscriptions(["BTC"], ["1m"])
        assert subs[0]["coin"] == "BTC"


# ----------------------------------------------------------------- REST out

class TestRestBackfillBoundary:
    @pytest.mark.asyncio
    async def test_backfill_requests_upstream_symbol_from_client(self, tmp_path):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        conn = init_db(str(tmp_path / "test.db"))
        candle_store = CandleStore(conn)
        client = AsyncMock()
        client.get_candle_snapshot.return_value = []

        await backfill_candles(client, candle_store, ["KPEPE"], ["1m"])

        client.get_candle_snapshot.assert_awaited_once()
        called_symbol = client.get_candle_snapshot.await_args.args[0]
        assert called_symbol == "kPEPE"

    @pytest.mark.asyncio
    async def test_backfill_stores_candles_under_internal_symbol(self, tmp_path):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        conn = init_db(str(tmp_path / "test.db"))
        candle_store = CandleStore(conn)
        client = AsyncMock()
        client.get_candle_snapshot.return_value = [
            {"t": 1000, "T": 1059999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "closed": True}
        ]

        await backfill_candles(client, candle_store, ["KPEPE"], ["1m"])

        df = candle_store.get_df("KPEPE", "1m")
        assert df is not None
        assert len(df) == 1
        # Never stored under the upstream/lowercase spelling.
        assert candle_store.get_df("kPEPE", "1m") is None


# --------------------------------------------------------------- inbound WS

class TestInboundNormalization:
    @pytest.mark.asyncio
    async def test_inbound_kpepe_candle_stores_under_uppercase(self, tmp_path, monkeypatch):
        conn = init_db(str(tmp_path / "test.db"))
        candle_store = CandleStore(conn)
        monkeypatch.setattr(main, "_candle_store", candle_store)

        msg = {
            "channel": "candle",
            "data": {
                "s": "kPEPE", "i": "1m",
                "t": 1000, "T": 1059999,
                "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "closed": True,
            },
        }
        await on_ws_message(msg)

        df = candle_store.get_df("KPEPE", "1m")
        assert df is not None
        assert len(df) == 1
        assert candle_store.get_df("kPEPE", "1m") is None


# ------------------------------------------------------------------ generic

class TestGenericNotPepeSpecific:
    """A synthetic, unrelated mixed-case asset proves the fix isn't hardcoded."""

    @pytest.mark.asyncio
    async def test_synthetic_mixed_case_asset_round_trips(self, tmp_path):
        conn = init_db(str(tmp_path / "test.db"))
        client = AsyncMock()
        client.get_meta_and_asset_ctxs.return_value = _meta_and_asset_ctxs(
            ["zFooBar"]
        )

        scan_symbols = await refresh_universe(conn, client, _settings())

        assert "ZFOOBAR" in scan_symbols
        assert get_upstream_symbol("ZFOOBAR") == "zFooBar"

        subs = build_ws_subscriptions(["ZFOOBAR"], ["1m"])
        assert subs[0]["coin"] == "zFooBar"


# -------------------------------------------------------------- collisions

class TestCollisionHandling:
    def test_conflicting_upstream_names_do_not_silently_overwrite(self, caplog):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        with caplog.at_level("WARNING", logger="apex.data.market_universe"):
            market_universe._record_upstream_symbol("Kpepe", "KPEPE")

        # First mapping wins; the conflict is reported, not swallowed.
        assert get_upstream_symbol("KPEPE") == "kPEPE"
        assert any("collision" in r.getMessage().lower() for r in caplog.records)

    def test_identical_repeated_mapping_is_not_a_collision(self, caplog):
        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        with caplog.at_level("WARNING", logger="apex.data.market_universe"):
            market_universe._record_upstream_symbol("kPEPE", "KPEPE")

        assert not any("collision" in r.getMessage().lower() for r in caplog.records)


# --------------------------------------------------------- DB compatibility

class TestDbAndSchemaCompatibility:
    @pytest.mark.asyncio
    async def test_existing_uppercase_db_rows_remain_queryable(self, tmp_path):
        """A row written before this fix (plain uppercase symbol) must still
        be reachable through the same internal-symbol lookups used today —
        this fix never changes what's written to `markets`/`candles`.
        """
        conn = init_db(str(tmp_path / "test.db"))
        candle_store = CandleStore(conn)
        # Simulate a pre-existing row written under the internal identity.
        candle_store.update(
            "KPEPE", "1m",
            {"t": 1000, "T": 1059999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "closed": True},
            persist=True,
        )

        fresh_store = CandleStore(conn)
        fresh_store.load_from_db("KPEPE", "1m")
        df = fresh_store.get_df("KPEPE", "1m")
        assert df is not None
        assert len(df) == 1

    def test_no_schema_migration_files_added(self):
        """Pinned-scope regression: verifies that the historical casing-fix
        commit 74e5da4 (Hyperliquid canonical symbol casing) touched neither
        schema.sql nor a migration file, since that fix was in-memory only
        (module-level map).

        The diff range is pinned to the fix's own commit range
        (f671f8d5..74e5da4), not open-ended against the current tree/HEAD —
        an earlier version of this test diffed against the working tree,
        which meant every future commit that ever touched schema.sql would
        fail this test, regardless of relevance to the casing fix.
        """
        import pathlib
        import subprocess

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        diff = subprocess.run(
            [
                "git", "diff", "--name-only",
                "f671f8d5acbbc6771d761ec87b8badc05b381e7a",
                "74e5da443de09e77637f4075424d827434ba2287",
            ],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout
        changed = set(diff.splitlines())
        assert "src/apex/db/schema.sql" not in changed
        assert not any("migrat" in f.lower() for f in changed)
