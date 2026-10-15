"""A disconnected connection's accounts vanish from every view.

While a connection is down nothing refreshes the accounts it serves —
quotes, P&L and quantities all freeze at their last values, which on
screen are indistinguishable from live ones.  So those accounts are
hidden outright: Positions, Positions Summary, Trades and Trades Summary
all drop them until the connection returns.

Nothing is DISCARDED.  Both position caches and the fill store keep their
entries and the views filter on the connection label every position and
fill already carries, so a reconnect restores each view exactly.  That
distinction matters most for fills: IB replays only a limited window, so
a fill actually deleted could never be recovered.

The known cost — a hidden position still exists at the broker, and a
short Positions tab can read as "I am flat" — is why the view always
announces what it hid.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.account_position_model import AccountPositionModel
from src.ui.widgets.offline_connections import hidden_accounts_notice
from src.ui.widgets.positions_summary_model import (
    S_COL_POSITION, S_COL_SYMBOL, PositionsSummaryModel)


def _pos(label, account, con_id=1, symbol="MNQU6", position=5.0):
    return {
        "label": label, "account": account, "con_id": con_id,
        "symbol": symbol, "local_symbol": symbol, "sec_type": "FUT",
        "exchange": "CME", "currency": "USD", "multiplier": "2",
        "position": position, "avg_cost": 29362.03,
        "market_price": 29373.80, "market_value": 293738.0,
        "unrealized_pnl": 235.0, "realized_pnl": 1390.0, "daily_pnl": 104.45,
    }


def _fill(label, account, exec_id, side="BOT", shares=1.0, price=29500.0):
    return {
        "exec_id": exec_id, "con_id": 1, "symbol": "MNQ",
        "local_symbol": "MNQU6", "sec_type": "FUT", "side": side,
        "shares": shares, "price": price, "exchange": "CME",
        "time_sort": "2026-08-21T10:00:00",
        "label": label, "account": account, "account_id": account,
    }


class TestFlatPositions:
    def _model(self):
        m = AccountPositionModel()
        m.update_positions([_pos("J-ib", "U111"), _pos("Q-ib", "U222")])
        return m

    def _accounts(self, m):
        return {m._rows[r].get("account") for r in range(m.rowCount())}

    def test_offline_accounts_disappear(self):
        m = self._model()
        assert self._accounts(m) == {"U111", "U222"}
        m.set_offline({"Q-ib"}, {"U222"})
        assert self._accounts(m) == {"U111"}

    def test_live_connection_is_untouched(self):
        m = self._model()
        m.set_offline({"Q-ib"}, {"U222"})
        rows = [m._rows[r] for r in range(m.rowCount())
                if m._rows[r].get("label")]
        assert rows and all(r["label"] == "J-ib" for r in rows)

    def test_account_band_is_suppressed_even_with_no_positions(self):
        """A header renders for any in-scope account with summary data,
        so without the accounts argument an offline account would still
        show a frozen Net / Excess / BP line with nothing beneath it."""
        m = AccountPositionModel()
        m.update_positions([_pos("J-ib", "U111")])
        m.set_account_data("U333", net_liq=1.0)
        assert "U333" in self._accounts(m)
        m.set_offline({"Q-ib"}, {"U333"})
        assert "U333" not in self._accounts(m)

    def test_reconnect_restores_everything(self):
        m = self._model()
        before = self._accounts(m)
        m.set_offline({"Q-ib"}, {"U222"})
        m.set_offline(set(), set())
        assert self._accounts(m) == before

    def test_nothing_is_discarded(self):
        """The cache keeps the rows — only the view filters."""
        m = self._model()
        m.set_offline({"Q-ib"}, {"U222"})
        assert any(a == "U222" for (a, _c) in m._positions)

    def test_hidden_accounts_reported(self):
        m = self._model()
        m.set_offline({"Q-ib"}, {"U222"})
        assert m.hidden_accounts() == {"U222"}

    def test_setter_is_idempotent(self):
        m = self._model()
        m.set_offline({"Q-ib"}, {"U222"})
        seen = []
        m.modelReset.connect(lambda: seen.append(1))
        m.set_offline({"Q-ib"}, {"U222"})
        assert seen == []


class TestAccountBandsAreSuppressed:
    """The regression: position rows filtered correctly but the account
    HEADER band stayed, showing frozen Net / Excess / BP with nothing
    beneath it.

    Bands render from ``_acct_data``, which carries no connection label,
    so the model cannot filter them by label the way it filters
    positions.  It therefore derives the offline account set itself
    rather than trusting the caller's list, which at startup is usually
    empty — ``accounts_detected`` fires on connect, typically before
    FlashTradeView has wired itself to the worker.
    """

    def _model(self):
        m = AccountPositionModel()
        m.update_positions([_pos("J-ib", "U111"), _pos("Q-ib", "U222")])
        for acct in ("U111", "U222", "U333"):
            m.set_account_data(acct, net_liq=207129.0, excess_liq=94917.0,
                               buying_power=312169.0)
        m.rebuild()
        return m

    def _accounts(self, m):
        return {m._rows[r].get("account") for r in range(m.rowCount())}

    def test_band_goes_even_with_no_account_list_supplied(self):
        """``set_offline`` called with labels only — the real startup
        case — must still suppress the band."""
        m = self._model()
        assert "U222" in self._accounts(m)
        m.set_offline({"Q-ib"})
        assert "U222" not in self._accounts(m)

    def test_derived_set_covers_accounts_that_hold_positions(self):
        m = self._model()
        m.set_offline({"Q-ib"})
        assert m.hidden_accounts() == {"U222"}

    def test_explicit_accounts_still_honoured_for_position_less_accounts(self):
        """An account with only summary data has no position to infer a
        label from, so the caller's list is the only way to reach it."""
        m = self._model()
        assert "U333" in self._accounts(m)
        m.set_offline({"Q-ib"}, {"U333"})
        assert "U333" not in self._accounts(m)

    def test_reconnect_restores_the_bands(self):
        m = self._model()
        before = self._accounts(m)
        m.set_offline({"Q-ib"}, {"U222", "U333"})
        m.set_offline(set(), set())
        assert self._accounts(m) == before


class TestSummaryPositions:
    def _model(self):
        m = PositionsSummaryModel()
        m.update_positions([
            _pos("J-ib", "U111", con_id=1),
            _pos("Q-ib", "U222", con_id=1),
            _pos("Q-ib", "U333", con_id=2, symbol="NQU6"),
        ])
        return m

    def _txt(self, m, row, col):
        return m.data(m.index(row, col), Qt.ItemDataRole.DisplayRole)

    def test_shared_contract_refolds_to_the_live_half(self):
        m = self._model()
        assert self._txt(m, m._order.index(1), S_COL_POSITION) == "+10"
        m.set_offline_labels({"Q-ib"})
        assert self._txt(m, m._order.index(1), S_COL_POSITION) == "+5"

    def test_contract_held_only_offline_drops_out(self):
        m = self._model()
        assert 2 in m._order
        m.set_offline_labels({"Q-ib"})
        assert 2 not in m._order

    def test_total_stays_a_plain_total(self):
        """Every remaining row is live, so the footer needs no caveat."""
        m = self._model()
        m.set_offline_labels({"Q-ib"})
        assert self._txt(m, m.rowCount() - 1, S_COL_SYMBOL) == "TOTAL"

    def test_reconnect_restores_the_aggregate(self):
        m = self._model()
        m.set_offline_labels({"Q-ib"})
        m.set_offline_labels(set())
        assert self._txt(m, m._order.index(1), S_COL_POSITION) == "+10"
        assert 2 in m._order


class TestTrades:
    def _panel(self, qtbot):
        from src.ui.widgets.trades_panel import TradesPanel
        from src.ui.widgets.trades_date_filter import TradeWindow
        from datetime import date
        p = TradesPanel()
        qtbot.addWidget(p)
        p._flat_proxy.setWindow(TradeWindow.unbounded())
        p._summary_model.set_window(TradeWindow.unbounded())
        p.add_fill(_fill("J-ib", "U111", "e1"))
        p.add_fill(_fill("Q-ib", "U222", "e2"))
        p.add_fill(_fill("Q-ib", "U222", "e3", side="SLD", price=29600.0))
        return p

    def test_offline_fills_leave_the_flat_view(self, qtbot):
        p = self._panel(qtbot)
        assert p._flat_proxy.rowCount() == 3
        p.set_offline_labels({"Q-ib"})
        assert p._flat_proxy.rowCount() == 1

    def test_offline_fills_leave_the_aggregates(self, qtbot):
        p = self._panel(qtbot)
        key = p._summary_model._order[0]
        assert p._summary_model._agg[key]["buys_qty"] == 2
        p.set_offline_labels({"Q-ib"})
        key = p._summary_model._order[0]
        assert p._summary_model._agg[key]["buys_qty"] == 1
        assert p._summary_model._agg[key]["sells_qty"] == 0

    def test_status_bar_totals_follow(self, qtbot):
        p = self._panel(qtbot)
        p.update_last_price("MNQ", 29500.0)
        before = p.pnl_totals(None)
        p.set_offline_labels({"Q-ib"})
        assert p.pnl_totals(None) != before

    def test_fills_are_filtered_not_deleted(self, qtbot):
        """IB replays only a limited window — a discarded fill could not
        be recovered, so reconnect must restore the blotter exactly."""
        p = self._panel(qtbot)
        p.set_offline_labels({"Q-ib"})
        assert set(p._summary_model._fills) == {"e1", "e2", "e3"}
        p.set_offline_labels(set())
        assert p._flat_proxy.rowCount() == 3
        key = p._summary_model._order[0]
        assert p._summary_model._agg[key]["buys_qty"] == 2


class TestNotice:
    def test_names_connection_and_count(self):
        msg = hidden_accounts_notice({"Q-ib"}, 2)
        assert "Q-ib" in msg and "2 accounts" in msg

    def test_singular(self):
        assert "1 account " in hidden_accounts_notice({"Q-ib"}, 1)

    def test_empty_when_nothing_offline(self):
        assert hidden_accounts_notice(set(), 0) == ""


class TestFlashTradeWiring:
    def _view(self, qtbot):
        from src.ui.widgets.flash_trade_view import FlashTradeView
        v = FlashTradeView()
        qtbot.addWidget(v)
        v._on_accounts_detected_for_visibility("J-ib", ["U111"])
        v._on_accounts_detected_for_visibility("Q-ib", ["U222", "U333"])
        v._account_position.update_positions([
            _pos("J-ib", "U111"), _pos("Q-ib", "U222", con_id=2)])
        v._trades_panel.add_fill(_fill("Q-ib", "U222", "e1"))
        return v

    def test_disconnect_hides_then_reconnect_restores(self, qtbot):
        v = self._view(qtbot)
        v._on_connection_status_for_visibility("Q-ib", False)
        assert v._account_position._model.offline_labels() == {"Q-ib"}
        assert v._trades_panel._flat_proxy.offlineLabels() == {"Q-ib"}
        v._on_connection_status_for_visibility("Q-ib", True)
        assert v._account_position._model.offline_labels() == set()
        assert v._trades_panel._flat_proxy.offlineLabels() == set()

    def test_accounts_come_from_the_detected_map(self, qtbot):
        """An explicit Disconnect clears AccountsTableModel's rows, so the
        account→connection mapping has to be held here instead."""
        v = self._view(qtbot)
        v._on_connection_status_for_visibility("Q-ib", False)
        assert v._account_position._model._offline_accounts == {"U222", "U333"}

    def test_accounts_also_come_from_the_session_registry(self, qtbot):
        """``accounts_detected`` fires on connect, usually before this
        view is wired to the worker — so at startup the map is empty and
        AccountsTableModel's registry has to supply the accounts."""
        from src.ui.widgets.accounts_model import AccountsTableModel
        from src.ui.widgets.flash_trade_view import FlashTradeView
        am = AccountsTableModel()
        am.update_batch([
            ("J-ib", "U111", "NetLiquidation", "1", "USD"),
            ("Q-ib", "U222", "NetLiquidation", "2", "USD"),
            ("Q-ib", "U999", "NetLiquidation", "3", "USD")])
        v = FlashTradeView()
        qtbot.addWidget(v)
        v._accounts_model = am
        assert v._label_accounts == {}          # nothing detected yet
        v._on_connection_status_for_visibility("Q-ib", False)
        assert v._account_position._model._offline_accounts == {"U222", "U999"}

    def test_it_announces_what_it_hid(self, qtbot):
        v = self._view(qtbot)
        seen = []
        v.transient_status_message.connect(lambda m, *_a: seen.append(m))
        v._on_connection_status_for_visibility("Q-ib", False)
        assert seen and "Q-ib" in seen[0] and "hidden" in seen[0]
        seen.clear()
        v._on_connection_status_for_visibility("Q-ib", True)
        assert seen and "reconnected" in seen[0]


class TestWorkerSwap:
    """Disconnect → Connect on the Conn tab replaces the worker.  The old
    one's shutdown ``connected=False`` reaches this view, but the new
    one's ``connected=True`` fires before the view wires it (on the next
    showEvent) — wiring must sync the real link state, or the
    connection's positions stay hidden until an app restart."""

    @staticmethod
    def _fake_worker(label, connected):
        from PySide6.QtCore import QObject, Signal

        class _FakeWorker(QObject):
            order_status_updated = Signal(object)
            portfolio_updated = Signal(object)
            flatten_close_needed = Signal(object)
            reverse_close_needed = Signal(object)
            pnl_single_updated = Signal(str, int, float, float, float)
            pnl_single_batch_updated = Signal(object)
            account_pnl_updated = Signal(str, float, float, float)
            fill_updated = Signal(object)
            connection_status_changed = Signal(str, bool)
            accounts_detected = Signal(str, object)
            price_updated = Signal(str, float)
            bg_price_updated = Signal(str, float, float, float, float)

            def __init__(self):
                super().__init__()
                self._label = label
                self.is_ib_connected = connected

            def request_order_snapshot(self): pass
            def request_portfolio_snapshot(self): pass
            def request_pnl_snapshot(self): pass
            def request_fills_snapshot(self): pass

        return _FakeWorker()

    def test_new_connected_worker_unhides_its_accounts(self, qtbot):
        from src.ui.widgets.flash_trade_view import FlashTradeView
        v = FlashTradeView()
        qtbot.addWidget(v)
        old = self._fake_worker("Q-ib", connected=True)
        v._ensure_worker_connected(old)
        old.connection_status_changed.emit("Q-ib", False)   # Disconnect
        assert v._account_position._model.offline_labels() == {"Q-ib"}

        new = self._fake_worker("Q-ib", connected=True)     # already up
        v._ensure_worker_connected(new)
        assert v._account_position._model.offline_labels() == set()
        assert v._trades_panel._flat_proxy.offlineLabels() == set()

    def test_still_connecting_worker_unhides_on_its_signal(self, qtbot):
        from src.ui.widgets.flash_trade_view import FlashTradeView
        v = FlashTradeView()
        qtbot.addWidget(v)
        v._on_connection_status_for_visibility("Q-ib", False)
        new = self._fake_worker("Q-ib", connected=False)
        v._ensure_worker_connected(new)
        assert v._account_position._model.offline_labels() == {"Q-ib"}
        new.connection_status_changed.emit("Q-ib", True)
        assert v._account_position._model.offline_labels() == set()
