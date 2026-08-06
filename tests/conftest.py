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
