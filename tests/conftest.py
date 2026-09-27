from __future__ import annotations

import pytest

from olist_platform.config import get_settings
from olist_platform.database.engine import get_engine


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Settings/engines are cached process-wide; reset so monkeypatched env applies."""
    _reset_caches()
    yield
    _reset_caches()
