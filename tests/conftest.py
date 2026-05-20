"""Shared pytest fixtures for APEX tests."""
from __future__ import annotations

import pytest

from apex.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Clear the get_settings() lru_cache before and after every test.

    get_settings() uses @lru_cache(maxsize=1). Without clearing, a test that
    calls get_settings() (e.g. via a script's main()) caches the Settings
    object for the rest of the test session — causing subsequent tests with
    different APEX_DB_PATH values to query the wrong database.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
