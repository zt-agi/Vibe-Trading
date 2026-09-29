"""ZT add-on: fixtures for the timing-contract acceptance suite."""

from __future__ import annotations

import pytest

from backtest.loaders.registry import LOADER_REGISTRY


@pytest.fixture(autouse=True)
def _restore_loader_registry():
    """pitdb registers itself on the first explicit request; undo that per test."""
    saved = dict(LOADER_REGISTRY)
    yield
    LOADER_REGISTRY.clear()
    LOADER_REGISTRY.update(saved)


@pytest.fixture(autouse=True)
def _no_guard_switch_from_the_shell(monkeypatch):
    """A developer's VIBE_TRADING_ASOF_GUARD must not change what the suite asserts."""
    monkeypatch.delenv("VIBE_TRADING_ASOF_GUARD", raising=False)
