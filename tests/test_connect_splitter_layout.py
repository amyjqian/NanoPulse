"""Connect page — the top/bottom splitter position persists across runs.

The view restores saved sizes at construction and saves (debounced) on
every handle drag, mirroring FlashTradeView's splitter persistence.
config_manager is monkeypatched so tests never touch the real
app_config.json.
"""
from __future__ import annotations

import pytest

import src.ui.widgets.connections_view as cv
from src.ui.widgets.accounts_model import AccountsTableModel


@pytest.fixture
def layout_store(monkeypatch):
    store = {"layout": {}}
    monkeypatch.setattr(cv.config_manager, "load_layout",
                        lambda path=None: dict(store["layout"]))

    def _save(layout, path=None):
        store["layout"] = dict(layout)
    monkeypatch.setattr(cv.config_manager, "save_layout", _save)
    # Connections are irrelevant here — keep construction hermetic.
    monkeypatch.setattr(cv.config_manager, "load_connections",
                        lambda path=None: [])
    return store


def _view(qtbot) -> cv.ConnectionsView:
    view = cv.ConnectionsView(AccountsTableModel(), {})
    qtbot.addWidget(view)
    view.resize(1000, 800)
    return view


def test_saved_sizes_restored_on_construction(qtbot, layout_store):
    layout_store["layout"] = {"connect_v_splitter": [222, 444]}
    view = _view(qtbot)
    # Sizes only materialise once the widget is laid out; Qt then scales
    # the stored request to the available height, so assert the restored
    # 1:2 proportion rather than exact pixels.
    view.show()
    qtbot.waitExposed(view)
    top, bottom = view._splitter.sizes()
    assert bottom > 0
    assert top / bottom == pytest.approx(222 / 444, abs=0.1)


def test_drag_persists_sizes(qtbot, layout_store):
    view = _view(qtbot)
    view._splitter.setSizes([300, 500])
    view._persist_splitter()          # what the debounce timer fires
    assert layout_store["layout"]["connect_v_splitter"] == \
        view._splitter.sizes()
    # Other layout keys survive the merge.
    layout_store["layout"]["flash_main_splitter"] = [1, 2, 3]
    view._splitter.setSizes([250, 550])
    view._persist_splitter()
    assert layout_store["layout"]["flash_main_splitter"] == [1, 2, 3]
    assert layout_store["layout"]["connect_v_splitter"] == \
        view._splitter.sizes()


def test_garbage_saved_sizes_fall_back_to_defaults(qtbot, layout_store):
    layout_store["layout"] = {"connect_v_splitter": [0, 0]}   # collapsed junk
    view = _view(qtbot)                # must not raise, defaults apply
    assert all(s >= 0 for s in view._splitter.sizes())
    layout_store["layout"] = {"connect_v_splitter": "nonsense"}
    _view(qtbot)                       # must not raise
