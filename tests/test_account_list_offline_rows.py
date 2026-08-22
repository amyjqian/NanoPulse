"""Account cards go offline the moment their connection drops.

Disconnecting a connection removes its accounts from AccountsTableModel
(``remove_rows_by_label``).  The Accounts panel is the SELECTOR, so —
unlike Positions and Trades, which hide offline accounts outright — its
cards stay visible: removing them would break group membership and hide
who an order fan-out is about to skip.  They render dimmed, ⚠-prefixed
and with their money blanked, because a balance nothing is refreshing is
indistinguishable on screen from a live one.

The bug this pins down: ``remove_rows_by_label`` used to rebuild
``_acct_to_label`` and drop ``_connected_labels`` only AFTER emitting its
row removals.  ``endRemoveRows`` runs receivers synchronously, and the
panel's handler re-resolves every row's connection state through those
very structures — so it read the removed accounts as still belonging to a
live label and immediately undid the offline styling ``_sync_rows`` had
just applied.  The cards kept their balances until something forced a
resync, which in practice meant switching account groups and back.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.account_list_panel import AccountListPanel
from src.ui.widgets.accounts_model import AccountsTableModel

_GROUP = ["U3333333", "U5555555", "U1111111", "U4444444"]


@pytest.fixture
def panel(qtbot):
    m = AccountsTableModel()
    m.update_batch([
        ("J-ib", "U3333333", "NetLiquidation", "207129", "USD"),
        ("J-ib", "U3333333", "TotalCashValue", "1136", "USD"),
        ("J-ib", "U5555555", "NetLiquidation", "153237", "USD"),
        ("Q-ib", "U1111111", "NetLiquidation", "268000", "USD"),
        ("Q-ib", "U1111111", "TotalCashValue", "70", "USD"),
        ("Q-ib", "U4444444", "NetLiquidation", "235342", "USD"),
    ])
    m.set_connection_status("J-ib", True)
    m.set_connection_status("Q-ib", True)
    p = AccountListPanel(m)
    qtbot.addWidget(p)
    p._groups = [{"name": "Gamma", "accounts": list(_GROUP),
                  "selected": list(_GROUP)}]
    p._active_group = "Gamma"
    p._sync_rows()
    return p


def _row(panel, account):
    return panel._account_rows[account]


class TestImmediateOfflineStyling:
    def test_cards_stay_visible(self, panel):
        """The panel is the selector — dropping cards would break group
        membership and hide who an order fan-out will skip."""
        panel._model.remove_rows_by_label("Q-ib")
        assert set(panel._account_rows) == set(_GROUP)

    def test_offline_cards_dim_without_a_group_switch(self, panel):
        """The regression: this used to need switching groups and back."""
        assert _row(panel, "U1111111")._connected is True
        panel._model.remove_rows_by_label("Q-ib")
        assert _row(panel, "U1111111")._connected is False
        assert _row(panel, "U4444444")._connected is False

    def test_warning_glyph_is_prefixed(self, panel):
        panel._model.remove_rows_by_label("Q-ib")
        assert _row(panel, "U1111111")._name_lbl.text().startswith("⚠ ")

    def test_money_is_blanked_not_frozen(self, panel):
        """A balance nothing refreshes reads exactly like a live one, and
        the account may have been traded elsewhere since."""
        assert _row(panel, "U1111111")._balance_lbl.text() == "268k"
        panel._model.remove_rows_by_label("Q-ib")
        assert _row(panel, "U1111111")._balance_lbl.text() == "–"
        assert _row(panel, "U1111111")._usd is None

    def test_live_connection_untouched(self, panel):
        panel._model.remove_rows_by_label("Q-ib")
        for acct in ("U3333333", "U5555555"):
            r = _row(panel, acct)
            assert r._connected is True
            assert r._balance_lbl.text() != "–"
            assert not r._name_lbl.text().startswith("⚠")

    def test_tooltip_explains_the_consequence(self, panel):
        panel._model.remove_rows_by_label("Q-ib")
        assert "skipped" in _row(panel, "U1111111").toolTip()


class TestDefaultGroups:
    """All / None / no-group used to let offline accounts vanish: with no
    membership list to diff against, they simply fell out of the model.
    ``known_accounts`` remembers them for the session instead."""

    @pytest.fixture
    def allgrp(self, panel):
        panel._select_default_group("All")
        return panel

    def test_offline_accounts_stay_listed(self, allgrp):
        allgrp._model.remove_rows_by_label("Q-ib")
        assert set(allgrp._account_rows) == set(_GROUP)
        assert _row(allgrp, "U1111111")._connected is False
        assert _row(allgrp, "U1111111")._balance_lbl.text() == "–"

    def test_none_group_behaves_the_same(self, panel):
        panel._select_default_group("None")
        panel._model.remove_rows_by_label("Q-ib")
        assert _row(panel, "U4444444")._connected is False

    def test_reconnect_restores_the_figures(self, allgrp):
        allgrp._model.remove_rows_by_label("Q-ib")
        allgrp._model.update_batch([
            ("Q-ib", "U1111111", "NetLiquidation", "268000", "USD")])
        allgrp._model.set_connection_status("Q-ib", True)
        r = _row(allgrp, "U1111111")
        assert r._connected is True
        assert r._balance_lbl.text() == "268k"
        assert not r._name_lbl.text().startswith("⚠")

    def test_hidden_accounts_do_not_reappear_when_they_go_offline(
            self, allgrp):
        """× hides an account from the default views; going offline must
        not smuggle it back in."""
        allgrp._hidden.add("U1111111")
        allgrp._sync_rows()
        assert "U1111111" not in allgrp._account_rows
        allgrp._model.remove_rows_by_label("Q-ib")
        assert "U1111111" not in allgrp._account_rows


class TestForgetLabel:
    """Deleting a connection must leave nothing behind — otherwise its
    accounts haunt the panel as permanent offline rows."""

    def test_delete_clears_the_offline_rows(self, panel):
        panel._select_default_group("All")
        panel._model.remove_rows_by_label("Q-ib")      # disconnect
        assert "U1111111" in panel._account_rows
        panel._model.forget_label("Q-ib")              # delete
        assert "U1111111" not in panel._account_rows
        assert "U4444444" not in panel._account_rows

    def test_delete_works_without_a_prior_disconnect(self, panel):
        panel._select_default_group("All")
        panel._model.forget_label("Q-ib")
        assert set(panel._account_rows) == {"U3333333", "U5555555"}

    def test_survivors_are_untouched(self, panel):
        panel._model.forget_label("Q-ib")
        assert panel._model.known_accounts() == {
            "U3333333": "J-ib", "U5555555": "J-ib"}

    def test_unknown_label_is_a_noop(self, panel):
        seen = []
        panel._model.known_accounts_changed.connect(lambda: seen.append(1))
        panel._model.forget_label("nope")
        assert seen == []

    def test_disconnect_keeps_accounts_known(self, panel):
        """The distinction the whole mechanism rests on."""
        panel._model.remove_rows_by_label("Q-ib")
        assert "U1111111" in panel._model.known_accounts()


class TestModelInvariant:
    """``remove_rows_by_label`` must present consistent derived state to
    anything reacting to its row removals."""

    def test_reverse_index_is_clear_during_the_removal_signal(self, panel):
        """The heart of the fix.  ``connection_for_account`` is what
        ``_is_account_connected`` consults first, so it must already
        return None while the removal is being announced — otherwise the
        panel resolves the account as live and re-styles it that way.

        (``is_label_connected`` is deliberately NOT asserted here: it
        defaults to True for labels it has never seen, so that a brand-new
        connection isn't dimmed before its first status signal.  The None
        check above is what makes a removed account read as offline.)
        """
        seen = {}
        panel._model.rowsRemoved.connect(
            lambda *_a: seen.setdefault(
                "label", panel._model.connection_for_account("U1111111")))
        panel._model.remove_rows_by_label("Q-ib")
        assert seen["label"] is None
        assert panel._model._connected_labels.get("Q-ib") is None

    def test_survivors_keep_their_mapping(self, panel):
        panel._model.remove_rows_by_label("Q-ib")
        assert panel._model.connection_for_account("U3333333") == "J-ib"
        assert panel._model.is_label_connected("J-ib") is True

    def test_unknown_label_is_a_noop(self, panel):
        before = dict(panel._model._acct_to_label)
        panel._model.remove_rows_by_label("nope")
        assert panel._model._acct_to_label == before

    def test_removing_every_label_empties_cleanly(self, panel):
        panel._model.remove_rows_by_label("J-ib")
        panel._model.remove_rows_by_label("Q-ib")
        assert panel._model._acct_to_label == {}
        assert all(r._connected is False
                   for r in panel._account_rows.values())
