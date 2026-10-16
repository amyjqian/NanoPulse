"""Shared test fixtures.

Every test runs against a fresh temp config file instead of the real
``app_config.json`` — panel/view constructors read defaults, and any
write path (chip lists, instrument metadata, layout, hot buttons) lands
in the throwaway file.  Individual tests can still repoint
``config_manager._active_path`` themselves; their monkeypatch simply
overrides this one.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _sandbox_config(tmp_path, monkeypatch):
    import src.core.config_manager as cm
    import src.core.instruments as instruments
    monkeypatch.setattr(cm, "_active_path", tmp_path / "app_config-test.json")
    instruments.invalidate_cache()
    yield
    instruments.invalidate_cache()


@pytest.fixture(autouse=True)
def _regular_hours_routing(monkeypatch):
    """Pin stock routing to regular hours: between 20:00 and 03:59 ET
    ``stk_routes_overnight`` turns stock pegs / Adaptive / market orders
    into limits, which would make order tests depend on the wall clock.
    Tests of the overnight path patch it back to True."""
    import src.core.ib_worker as ib_worker
    monkeypatch.setattr(ib_worker, "stk_routes_overnight",
                        lambda currency="USD", now=None: False)


@pytest.fixture(autouse=True)
def _sandbox_pending_exits(tmp_path, monkeypatch):
    """Overnight pending exits go to a throwaway file, never data/."""
    import src.core.pending_exits as pending_exits
    monkeypatch.setattr(pending_exits, "_path",
                        tmp_path / "pending_exits.json")
