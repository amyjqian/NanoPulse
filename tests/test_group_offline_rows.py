"""A user group must show ALL its members — including accounts whose
connection never came up this session (absent from the accounts model).
Those render as dimmed offline rows instead of silently disappearing,
so the user can see who an order fan-out will skip."""
from __future__ import annotations

from src.core import config_manager
from src.ui.widgets.account_list_panel import AccountListPanel
from src.ui.widgets.accounts_model import AccountsTableModel


def _make_panel(qtbot, groups: list[dict]) -> AccountListPanel:
    config_manager.save_account_groups(groups)
    am = AccountsTableModel()
    # One live connection reporting a single account.
    am.update_batch([("L1", "DU111", "NetLiquidation", "998000", "USD")])
    panel = AccountListPanel(am)
    qtbot.addWidget(panel)
    panel._model_pin = am  # pin: pytest-qt holds widgets weakly
    return panel


_BETA = {
    "name": "Beta",
    "accounts": ["DU111", "DUP626261", "U3333333"],
    "selected": ["DU111", "DUP626261", "U3333333"],
}


class TestOfflineGroupMembers:
    def test_group_shows_offline_members_dimmed(self, qtbot):
        panel = _make_panel(qtbot, [_BETA])
        panel._select_group("Beta")
        assert set(panel._account_rows) == set(_BETA["accounts"])
        assert panel._account_rows["DU111"]._connected is True
        for offline in ("DUP626261", "U3333333"):
            row = panel._account_rows[offline]
            assert row._connected is False
            assert row._name_lbl.text().startswith("⚠")

    def test_offline_row_heals_when_connection_arrives(self, qtbot):
        panel = _make_panel(qtbot, [_BETA])
        panel._select_group("Beta")
        assert panel._account_rows["DUP626261"]._connected is False
        # The second connection comes up and reports the account.
        panel._model.update_batch(
            [("L2", "DUP626261", "NetLiquidation", "500000", "USD")])
        panel._sync_rows()
        row = panel._account_rows["DUP626261"]
        assert row._connected is True
        assert not row._name_lbl.text().startswith("⚠")

    def test_default_views_unchanged_no_phantom_rows(self, qtbot):
        # "All" / no-group views list detected accounts only — offline
        # group members belong to the group view, not the global list.
        panel = _make_panel(qtbot, [_BETA])
        panel._select_default_group("All")
        assert set(panel._account_rows) == {"DU111"}

    def test_offline_member_keeps_group_check_state(self, qtbot):
        panel = _make_panel(qtbot, [_BETA])
        panel._select_group("Beta")
        # In the group's selected list → checked, even while offline
        # (the fan-out itself warns and skips disconnected accounts).
        assert panel._account_rows["DUP626261"].is_checked is True


class TestEmptiedGroupPersists:
    """Removing every member from a group must NOT delete the group —
    only an explicit right-click → Delete on the pill does that."""

    def test_removing_last_member_keeps_group_active(self, qtbot):
        one = {"name": "Solo", "accounts": ["DU111"],
               "selected": ["DU111"]}
        panel = _make_panel(qtbot, [one])
        panel._select_group("Solo")
        panel._remove_account_from_active_group("DU111")
        g = panel._find_group("Solo")
        assert g is not None and g["accounts"] == []
        # Still the active group, pill still present, list just empty.
        assert panel._active_group == "Solo"
        assert "Solo" in panel._group_pills
        assert panel._account_rows == {}

    def test_empty_group_survives_config_round_trip(self, qtbot):
        one = {"name": "Solo", "accounts": ["DU111"],
               "selected": ["DU111"]}
        panel = _make_panel(qtbot, [one])
        panel._select_group("Solo")
        panel._remove_account_from_active_group("DU111")
        loaded = config_manager.load_account_groups()
        assert [g["name"] for g in loaded] == ["Solo"]
        assert loaded[0]["accounts"] == []

    def test_member_can_rejoin_emptied_group(self, qtbot):
        one = {"name": "Solo", "accounts": ["DU111"],
               "selected": ["DU111"]}
        panel = _make_panel(qtbot, [one])
        panel._select_group("Solo")
        panel._remove_account_from_active_group("DU111")
        # Re-adding via the Add-account path re-joins the active group.
        panel._add_combo.addItem("DU111", "DU111")
        panel._on_add_account(panel._add_combo.count() - 1)
        g = panel._find_group("Solo")
        assert g["accounts"] == ["DU111"]
        assert g["selected"] == ["DU111"]
