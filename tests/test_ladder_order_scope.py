"""Ladder order badges follow the active account group.

The Book Trader ladder tracks every worker's open orders, but the
accounts panel scopes what the page is *about*: with a group active, an
order resting for an account outside the group (e.g. mgn-zara-08 while
Gamma is selected) must get no badge — and, just as important, a ladder
click must not cancel or "move" it.  The host pushes the scope via
``set_account_scope_provider`` (FlashTradeView wires it to
``AccountListPanel.get_visible_accounts``); no provider, or a provider
returning None, means unscoped — every order badges as before.
"""
from __future__ import annotations

import pytest

import src.ui.widgets.book_trader_view as btv


def _order(**over) -> dict:
    base = {
        "order_id": "U1_nano_260904_100000_abcd",
        "order_ref": "nano_260904_100000_abcd",
        "symbol": "AAPL", "con_id": 0, "sec_type": "",
        "account": "U1", "action": "BUY", "order_type": "LMT",
        "quantity": "5", "limit_price": "100", "stop_price": "–",
        "tif": "DAY", "fill_price": "–", "status": "Submitted",
        "time": "10:00:00", "time_sort": "2026-09-04T10:00:00",
        "error_message": "",
    }
    base.update(over)
    return base


@pytest.fixture
def view(qtbot, monkeypatch):
    import src.core.instruments as instruments
    monkeypatch.setattr(instruments, "get_contract", lambda _q: None)
    v = btv.BookTraderView(active_threads={})
    qtbot.addWidget(v)
    v._active_symbol = "AAPL"
    return v


def _badge_prices(v) -> dict[float, list[dict]]:
    return dict(v._model._orders)


class TestBadgeScope:
    def test_unscoped_shows_every_account(self, view):
        view._on_order_status(_order())
        view._on_order_status(_order(order_id="U9_x", account="U9",
                                     limit_price="101"))
        assert len(_badge_prices(view)) == 2

    def test_out_of_scope_account_gets_no_badge(self, view):
        view.set_account_scope_provider(lambda: {"U1"})
        view._on_order_status(_order())
        view._on_order_status(_order(order_id="U9_x", account="U9",
                                     limit_price="101"))
        prices = _badge_prices(view)
        assert len(prices) == 1
        assert 100.0 in prices

    def test_scope_change_reprojects(self, view):
        scope = {"U1", "U9"}
        view.set_account_scope_provider(lambda: scope)
        view._on_order_status(_order())
        view._on_order_status(_order(order_id="U9_x", account="U9",
                                     limit_price="101"))
        assert len(_badge_prices(view)) == 2
        scope.clear()
        scope.add("U1")
        view.refresh_order_badges()
        assert len(_badge_prices(view)) == 1

    def test_none_scope_means_unscoped(self, view):
        view.set_account_scope_provider(lambda: None)
        view._on_order_status(_order(account="U9"))
        assert len(_badge_prices(view)) == 1

    def test_missing_account_fails_open(self, view):
        """Never hide an order because a payload field went missing."""
        view.set_account_scope_provider(lambda: {"U1"})
        view._on_order_status(_order(order_id="anon_x", account=""))
        assert len(_badge_prices(view)) == 1

    def test_broken_provider_fails_open(self, view):
        def boom():
            raise RuntimeError("panel gone")
        view.set_account_scope_provider(boom)
        view._on_order_status(_order(account="U9"))
        assert len(_badge_prices(view)) == 1


class TestClickScope:
    """A click must act only on in-scope orders: the out-of-scope order
    the ladder doesn't show must be neither toggled off (click on its
    rung) nor cancelled by the move-order flow (click elsewhere)."""

    def test_click_on_rung_does_not_cancel_out_of_scope(self, view, qtbot):
        view.set_account_scope_provider(lambda: {"U1"})
        view._on_order_status(_order(order_id="U9_x", account="U9",
                                     limit_price="100"))
        cancelled: list[str] = []
        view.cancel_order_requested.connect(cancelled.append)
        placed: list[dict] = []
        view.order_requested.connect(placed.append)
        view._on_ladder_clicked("BUY", 100.0)
        assert cancelled == []
        assert len(placed) == 1          # fresh order placed instead

    def test_move_does_not_cancel_out_of_scope(self, view):
        view.set_account_scope_provider(lambda: {"U1"})
        view._on_order_status(_order(order_id="U9_x", account="U9",
                                     limit_price="100"))
        cancelled: list[str] = []
        view.cancel_order_requested.connect(cancelled.append)
        view._on_ladder_clicked("BUY", 105.0)
        assert cancelled == []

    def test_in_scope_toggle_still_cancels(self, view):
        view.set_account_scope_provider(lambda: {"U1"})
        view._on_order_status(_order())
        cancelled: list[str] = []
        view.cancel_order_requested.connect(cancelled.append)
        view._on_ladder_clicked("BUY", 100.0)
        assert cancelled == ["U1_nano_260904_100000_abcd"]
