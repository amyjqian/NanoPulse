"""Group-pill drag reorder: the dragged pill takes the drop target's
position, so a drag works in ANY direction — left→right, right→left,
and across rows (the grid is row-major, so up/down are just indices)."""
from __future__ import annotations

from src.core import config_manager
from src.ui.widgets.account_list_panel import AccountListPanel
from src.ui.widgets.accounts_model import AccountsTableModel


def _make_panel(qtbot):
    groups = [{"name": n, "accounts": [f"DU{i}"], "selected": [f"DU{i}"]}
              for i, n in enumerate(("Alpha", "Beta", "Gamma", "Core"))]
    config_manager.save_account_groups(groups)
    am = AccountsTableModel()
    panel = AccountListPanel(am)
    qtbot.addWidget(panel)
    panel._model_pin = am  # pin: pytest-qt holds widgets weakly
    return panel


def _names(panel) -> list[str]:
    return [g["name"] for g in panel._groups]


def test_drag_forward_lands_after_target(qtbot):
    # Alpha dropped on Gamma (left → right): Alpha takes Gamma's slot.
    panel = _make_panel(qtbot)
    panel._on_group_drop("Alpha", "Gamma")
    assert _names(panel) == ["Beta", "Gamma", "Alpha", "Core"]


def test_drag_to_last_position_reachable(qtbot):
    panel = _make_panel(qtbot)
    panel._on_group_drop("Alpha", "Core")
    assert _names(panel) == ["Beta", "Gamma", "Core", "Alpha"]


def test_drag_onto_immediate_right_neighbor_swaps(qtbot):
    # Previously a no-op (insert-before semantics).
    panel = _make_panel(qtbot)
    panel._on_group_drop("Alpha", "Beta")
    assert _names(panel) == ["Beta", "Alpha", "Gamma", "Core"]


def test_drag_backward_lands_before_target(qtbot):
    panel = _make_panel(qtbot)
    panel._on_group_drop("Core", "Alpha")
    assert _names(panel) == ["Core", "Alpha", "Beta", "Gamma"]


def test_reorder_persists_to_config(qtbot):
    panel = _make_panel(qtbot)
    panel._on_group_drop("Alpha", "Core")
    saved = [g["name"] for g in config_manager.load_account_groups()]
    assert saved == ["Beta", "Gamma", "Core", "Alpha"]


def test_unknown_names_ignored(qtbot):
    panel = _make_panel(qtbot)
    panel._on_group_drop("Nope", "Beta")
    panel._on_group_drop("Beta", "Nope")
    assert _names(panel) == ["Alpha", "Beta", "Gamma", "Core"]
