"""Column choices on the Trades / Trades Summary views survive a restart.

The gear opens Preferences → Columns, whose Apply writes the visible-key
list into ``preferences.trades_columns``.  On the next launch TradesPanel
reads it back and also surfaces columns the config predates — that
migration is what used to undo removals: it re-added any column missing
from the visible list, so taking one out of the Summary view lasted only
until the app restarted.  It now diffs against a ``known`` stamp instead,
so it can tell "never existed when you saved" from "you removed it".
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QWidget

from src.core import config_manager
from src.ui.widgets.trades_panel import TradesPanel


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point config_manager at a throwaway config file."""
    path = tmp_path / "app_config-test.json"
    monkeypatch.setattr(config_manager, "_default_path", lambda: path)
    return path


def _write(cfg, trades_columns):
    cfg.write_text(json.dumps({"preferences":
                               {"trades_columns": trades_columns}}))


def _read(cfg):
    return json.loads(cfg.read_text())["preferences"]["trades_columns"]


def _remove(panel, kind, key):
    panel.apply_columns(
        kind, [k for k in panel.column_state(kind)[2] if k != key])
    panel._save_persisted_columns()          # skip the 350 ms debounce


@pytest.mark.parametrize("kind, key", [
    ("flat", "exchange"),
    # sec_type is on the legacy migration table — the case that regressed.
    ("summary", "sec_type"),
    ("summary", "trans_total"),
    ("summary", "unpaired_pnl"),
])
def test_removed_column_stays_removed_across_a_restart(qtbot, cfg, kind, key):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    assert key in panel.column_state(kind)[2]
    _remove(panel, kind, key)

    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    assert key not in restarted.column_state(kind)[2]


def test_legacy_config_without_the_stamp_migrates_once(qtbot, cfg):
    """A config written before the stamp still gains the newer summary
    columns — but only on that one launch."""
    _write(cfg, {
        "style_v": 2,
        "flat": {"visible": ["time", "symbol", "side", "qty", "price"]},
        "summary": {"visible": ["symbol", "buys", "sells", "net",
                                "avg_buy", "avg_sell", "net_total"]},
    })

    panel = TradesPanel()
    qtbot.addWidget(panel)
    visible = panel.column_state("summary")[2]
    for key in ("sec_type", "trans_total", "paired_pnl", "unpaired_pnl"):
        assert key in visible
    # The stamp is written straight away, not on the next Apply.
    assert "known" in _read(cfg)["summary"]

    _remove(panel, "summary", "sec_type")
    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    assert "sec_type" not in restarted.column_state("summary")[2]


def test_a_genuinely_new_column_is_surfaced_in_model_order(qtbot, cfg):
    """A column absent from the stamp never existed when the user saved,
    so it appears — beside its model neighbour, not appended at the end."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel._save_persisted_columns()

    trades_columns = _read(cfg)
    for section in ("flat", "summary"):
        trades_columns[section]["known"] = [
            k for k in trades_columns[section]["known"] if k != "qty"]
        trades_columns[section]["visible"] = [
            k for k in trades_columns[section]["visible"] if k != "qty"]
    trades_columns["summary"]["known"] = [
        k for k in trades_columns["summary"]["known"] if k != "net"]
    trades_columns["summary"]["visible"] = [
        k for k in trades_columns["summary"]["visible"] if k != "net"]
    _write(cfg, trades_columns)

    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    flat_visible = restarted.column_state("flat")[2]
    assert flat_visible[flat_visible.index("side") + 1] == "qty"
    summary_visible = restarted.column_state("summary")[2]
    assert summary_visible[summary_visible.index("sells") + 1] == "net"
    # "qty" is not a summary column, so it must not leak into that view.
    assert "qty" not in summary_visible


# ── The Preferences dialog must not roll back what Apply just saved ─────────

@pytest.fixture
def open_dialog(qtbot):
    """The gear opens a fresh PreferencesDialog, which snapshots the whole
    preferences dict at construction.

    The host widget is kept alive by the fixture: qtbot holds only weak
    references, so a local parent would be collected mid-test and take
    the dialog's C++ object with it.
    """
    from src.ui.widgets.preferences_dialog import PreferencesDialog
    kept = []

    def _open(provider):
        host = QWidget()
        qtbot.addWidget(host)
        dlg = PreferencesDialog(host, columns_provider=provider)
        qtbot.addWidget(dlg)
        kept.append((host, dlg))
        return dlg

    yield _open
    for host, dlg in kept:
        dlg.setParent(None)


@pytest.mark.parametrize("kind, key", [
    ("flat", "exchange"),
    ("summary", "exchanges"),
])
def test_closing_preferences_keeps_the_column_removal(
        qtbot, cfg, open_dialog, kind, key):
    """Apply persists through the panel while the dialog is open; the
    dialog's save-on-close used to write its pre-Apply snapshot back."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel._save_persisted_columns()

    dlg = open_dialog(panel)
    _remove(panel, kind, key)
    dlg.close()

    assert key not in _read(cfg)[kind]["visible"]
    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    assert key not in restarted.column_state(kind)[2]


def test_closing_preferences_keeps_other_sections_written_meanwhile(
        qtbot, cfg, open_dialog):
    """Same clobber, other victims: anything saved while the dialog is
    open (positions columns, the hourly auto-backup stamp) must survive."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    dlg = open_dialog(panel)

    prefs = config_manager.load_preferences()
    prefs["positions_columns"] = {"flat": {"visible": ["symbol", "position"]}}
    prefs["backup"] = {"last_auto": "2026-08-24T10:00:00"}
    config_manager.save_preferences(prefs)

    dlg.close()

    saved = config_manager.load_preferences()
    assert saved["positions_columns"] == {
        "flat": {"visible": ["symbol", "position"]}}
    assert saved["backup"] == {"last_auto": "2026-08-24T10:00:00"}


def test_closing_preferences_still_writes_its_own_sections(
        qtbot, cfg, open_dialog):
    """The merge must not cost the dialog its own save."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    dlg = open_dialog(panel)
    dlg.close()

    saved = config_manager.load_preferences()
    for section in ("display", "trading_futures", "trading_stocks",
                    "trading_guards", "cash_management"):
        assert section in saved


# ── Column widths ───────────────────────────────────────────────────────────

def _drag_column(table, logical, width):
    """A separator drag goes through resizeSection, which is what emits
    sectionResized and starts the debounce."""
    table.horizontalHeader().resizeSection(logical, width)


@pytest.mark.parametrize("kind", ["flat", "summary"])
def test_a_column_resize_survives_a_restart(qtbot, cfg, kind):
    from src.ui.widgets.trades_model import COL_QTY
    from src.ui.widgets.trades_summary_model import S_COL_BUYS

    panel = TradesPanel()
    qtbot.addWidget(panel)
    table, logical, key = ((panel._flat_table, COL_QTY, "qty")
                           if kind == "flat"
                           else (panel._summary_table, S_COL_BUYS, "buys"))
    _drag_column(table, logical, 222)
    panel._save_timer.stop()             # stand in for the debounce firing
    panel._save_persisted_columns()

    assert _read(cfg)[kind]["widths"][key] == 222
    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    restarted_table = (restarted._flat_table if kind == "flat"
                       else restarted._summary_table)
    assert restarted_table.horizontalHeader().sectionSize(logical) == 222


def test_a_resize_just_before_quitting_is_not_lost(qtbot, cfg):
    """The write is debounced by 350 ms, but a QTimer needs an event loop
    and MainWindow.closeEvent blocks in worker.wait() then exits — so the
    host has to drain the pending save explicitly."""
    from src.ui.widgets.trades_summary_model import S_COL_BUYS

    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel._save_persisted_columns()
    _drag_column(panel._summary_table, S_COL_BUYS, 222)
    assert panel._save_timer.isActive(), "resize should arm the debounce"

    panel.flush_pending_column_state()

    assert not panel._save_timer.isActive()
    assert _read(cfg)["summary"]["widths"]["buys"] == 222
    restarted = TradesPanel()
    qtbot.addWidget(restarted)
    assert restarted._summary_table.horizontalHeader().sectionSize(
        S_COL_BUYS) == 222


def test_the_panel_drains_itself_on_hide_and_close(qtbot, cfg):
    """Switching away from the Trades tab, or closing the panel, must not
    strand a pending resize either."""
    from src.ui.widgets.trades_summary_model import S_COL_BUYS

    for act in ("hide", "close"):
        panel = TradesPanel()
        qtbot.addWidget(panel)
        panel.show()
        panel._save_persisted_columns()
        _drag_column(panel._summary_table, S_COL_BUYS, 333)
        getattr(panel, act)()
        assert _read(cfg)["summary"]["widths"]["buys"] == 333, act


def test_main_window_flushes_the_trades_panel_on_close():
    """The panel is a grandchild of MainWindow, and Qt sends closeEvent
    only to the top-level widget — so the host must call the flush."""
    import inspect
    from src.ui.main_window import MainWindow
    src = inspect.getsource(MainWindow.closeEvent)
    assert "flush_pending_column_state" in src
