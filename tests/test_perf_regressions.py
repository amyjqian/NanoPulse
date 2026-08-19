"""Operation-count regressions for the performance overhaul.

These are deterministic counters, not timings: they pin the caching /
batching contracts so a future change can't quietly reintroduce the
walk-per-repaint and restyle-per-tick behavior that made the app slow
over a long session.

Contracts pinned here:
* Trades summary FIFO walks run once per key and are cached — repaints
  and price ticks perform ZERO walks; ``add_fill`` re-walks only its
  key; every filter setter drops the whole cache.
* ``pnl_totals`` memoizes its price-independent half: repeat calls scan
  no fills, yet still follow live price marks.
* The blotter models keep their id→row maps consistent under appends,
  out-of-order inserts, updates and removals (the ascending-internal /
  descending-view storage).
* ``_on_global_order_status`` skips handled orders BEFORE building the
  trade dict, and the fast key derivation matches ``_build_trade_dict``.
* AccountListPanel row widgets skip ``setStyleSheet`` (a full QSS
  re-polish) when nothing changed.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.table_theme import SortValueRole
from src.ui.widgets.trades_summary_model import TradesSummaryModel

_DAY = "2026-05-29T10:00:00"


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    pinned = date(2026, 5, 29)
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)


@pytest.fixture
def walk_counter(monkeypatch):
    """Count every ``_walk_fills`` invocation (cache misses + pnl_totals)."""
    counter = {"n": 0}
    real = TradesSummaryModel._walk_fills.__func__

    def counting(cls, fills):
        counter["n"] += 1
        return real(cls, fills)

    monkeypatch.setattr(TradesSummaryModel, "_walk_fills",
                        classmethod(counting))
    return counter


def _fill(exec_id, side, shares, price, *, con_id=1, symbol="AAA",
          account="U1", t=_DAY):
    return {
        "exec_id": exec_id, "con_id": con_id, "symbol": symbol,
        "local_symbol": symbol, "sec_type": "STK", "side": side,
        "shares": shares, "price": price, "exchange": "NASDAQ",
        "time_sort": t, "account_id": account,
    }


def _model(n_keys=4, fills_per_key=6):
    m = TradesSummaryModel()
    i = 0
    for k in range(n_keys):
        for j in range(fills_per_key):
            i += 1
            m.add_fill(_fill(
                f"e{i}", "BOT" if j % 2 else "SLD", 1 + j, 100.0 + j,
                con_id=k + 1, symbol=f"SY{k}",
                t=f"2026-05-29T10:{i % 60:02d}:00"))
    return m


def _sweep(m):
    """Emulate a repaint: the roles the delegates pull, every cell."""
    for r in range(m.rowCount()):
        for c in range(m.columnCount()):
            idx = m.index(r, c)
            for role in (Qt.ItemDataRole.DisplayRole,
                         Qt.ItemDataRole.DisplayRole,
                         Qt.ItemDataRole.ForegroundRole):
                m.data(idx, role)
            m.data(idx, SortValueRole)


class TestSummaryWalkCache:
    def test_warm_sweep_performs_zero_walks(self, walk_counter):
        m = _model()
        _sweep(m)                       # warms the per-key cache
        walk_counter["n"] = 0
        _sweep(m)
        assert walk_counter["n"] == 0

    def test_price_tick_performs_zero_walks(self, walk_counter):
        m = _model()
        _sweep(m)
        walk_counter["n"] = 0
        m.set_last_price("SY0", 123.0)  # SY0 has an open leg
        _sweep(m)
        assert walk_counter["n"] == 0

    def test_add_fill_rewalks_only_its_key(self, walk_counter):
        m = _model(n_keys=4)
        _sweep(m)
        walk_counter["n"] = 0
        m.add_fill(_fill("new1", "BOT", 1, 105.0, con_id=1, symbol="SY0",
                         t="2026-05-29T11:00:00"))
        _sweep(m)
        assert walk_counter["n"] == 1   # SY0's key only

    @pytest.mark.parametrize("setter,arg", [
        ("set_days_back", 2),
        ("set_search_text", "SY"),
        ("set_selected_accounts", {"U1"}),
        ("set_hidden_fills", {"e1"}),
    ])
    def test_filter_setters_drop_the_cache(self, walk_counter, setter, arg):
        m = _model(n_keys=4)
        _sweep(m)
        walk_counter["n"] = 0
        getattr(m, setter)(arg)
        _sweep(m)
        assert walk_counter["n"] >= 1   # cache was really invalidated
        walk_counter["n"] = 0
        _sweep(m)                       # …and re-warmed
        assert walk_counter["n"] == 0


class TestPnlTotalsMemo:
    def test_repeat_call_scans_nothing_and_matches(self, walk_counter):
        m = _model()
        first = m.pnl_totals()
        walk_counter["n"] = 0
        assert m.pnl_totals() == first
        assert walk_counter["n"] == 0

    def test_price_tick_updates_u_without_a_scan(self, walk_counter):
        m = _model(n_keys=1, fills_per_key=3)   # odd count → open leg
        before = m.pnl_totals()
        walk_counter["n"] = 0
        m.set_last_price("SY0", 250.0)
        after = m.pnl_totals()
        assert walk_counter["n"] == 0
        assert after[1] != before[1]            # U followed the mark
        assert after[0] == before[0]            # R untouched

    def test_new_fill_invalidates(self, walk_counter):
        m = _model()
        m.pnl_totals()
        m.add_fill(_fill("nf", "BOT", 1, 100.0, con_id=9, symbol="NEW",
                         t="2026-05-29T12:00:00"))
        walk_counter["n"] = 0
        m.pnl_totals()
        assert walk_counter["n"] >= 1


class TestTradesModelIndex:
    def _consistent(self, m):
        # Every exec_id maps to the internal slot holding its dict, and
        # the view reads newest-first.
        for eid, pos in m._index.items():
            assert m._rows[pos].get("exec_id") == eid
        views = [m.row_data(r).get("time_sort") for r in range(m.rowCount())]
        assert views == sorted(views, reverse=True)

    def test_appends_updates_and_out_of_order(self):
        from src.ui.widgets.trades_model import TradesModel
        m = TradesModel()
        for i in range(10):
            m.add_fill(_fill(f"e{i}", "BOT", 1, 100.0,
                             t=f"2026-05-29T10:{i:02d}:00"))
        # Out-of-order (reconnect-style late fill)
        m.add_fill(_fill("late", "SLD", 1, 101.0, t="2026-05-29T10:03:30"))
        # In-place update of an existing exec_id
        m.add_fill(_fill("e5", "BOT", 7, 99.0, t="2026-05-29T10:05:00"))
        self._consistent(m)
        assert m.rowCount() == 11
        assert m.row_data(0)["exec_id"] == "e9"          # newest on top
        assert m.row_data(m.rowCount() - 1)["exec_id"] == "e0"


class TestOrderStatusModelIndex:
    def _order_dict(self, i, status="Submitted"):
        return {
            "order_ref": f"nano_260529_{i:06d}_abcd", "symbol": "SY0",
            "account": "U1", "action": "BUY", "order_type": "LMT",
            "quantity": "1", "limit_price": "100", "stop_price": "-",
            "tif": "DAY", "fill_price": "-", "status": status,
            "time": "10:00:00",
            "time_sort": f"2026-05-29T10:{i % 60:02d}:00",
        }

    def test_interleaved_ops_keep_map_and_view_consistent(self):
        from src.ui.widgets.order_status_model import OrderStatusModel
        m = OrderStatusModel()
        for i in range(8):
            m.add_order(f"oid{i}", self._order_dict(i))
        m.update_order("oid3", self._order_dict(3, status="Filled"))
        m.remove_order("oid5")
        for oid, pos in m._order_map.items():
            assert m._orders[pos].get("order_id") == oid
        views = [m.order_at(r).get("time_sort") for r in range(m.rowCount())]
        assert views == sorted(views, reverse=True)
        assert m.order_id_at(0) == "oid7"                # newest on top
        assert m.order_status("oid3") == "Filled"
        assert not m.has_order("oid5")


class TestGlobalOrderStatusSkip:
    def _trade(self, ref="nano_260529_100000_abcd", acct="U1"):
        order = SimpleNamespace(orderRef=ref, account=acct,
                                permId=42, orderId=7)
        status = SimpleNamespace(status="Submitted", permId=42)
        return SimpleNamespace(order=order, orderStatus=status)

    def test_key_derivations_match(self):
        from src.core.ib_worker import IBWorkerThread
        key = IBWorkerThread._trade_order_key
        t = self._trade()
        assert key(t.order, t.orderStatus) == "U1_nano_260529_100000_abcd"
        t2 = self._trade(ref="")            # external → permId key
        assert key(t2.order, t2.orderStatus) == "U1_p42"
        t3 = self._trade(ref="")
        t3.order.permId = 0
        t3.orderStatus.permId = 0
        assert key(t3.order, t3.orderStatus) == "U1_7"   # orderId fallback

    def test_handled_order_skips_dict_build(self):
        from src.core.ib_worker import IBWorkerThread
        t = self._trade()
        fake = SimpleNamespace(
            _has_status_handler={"U1_nano_260529_100000_abcd"},
            _trade_order_key=IBWorkerThread._trade_order_key,
            _build_trade_dict=lambda trade: pytest.fail(
                "handled order must not build the trade dict"),
        )
        IBWorkerThread._on_global_order_status(fake, t)   # must return early


class TestAccountRowRestyleGuards:
    def test_set_balance_skips_restyle_when_unchanged(self, qtbot):
        from src.ui.widgets.accounts_model import AccountsTableModel
        from src.ui.widgets.account_list_panel import AccountListPanel
        am = AccountsTableModel()
        am.update_batch([("L", "U1", "NetLiquidation", "1000", "USD")])
        p = AccountListPanel(am)
        qtbot.addWidget(p)
        row = p._account_rows["U1"]
        calls = {"n": 0}
        real = row._balance_lbl.setStyleSheet
        row._balance_lbl.setStyleSheet = (
            lambda ss: (calls.__setitem__("n", calls["n"] + 1), real(ss))[1])
        row.set_balance("1000")
        first = calls["n"]
        row.set_balance("1000")           # identical → no restyle
        assert calls["n"] == first
        row.set_balance("2000")           # changed → restyles
        assert calls["n"] == first + 1

    def test_set_name_reports_changes(self, qtbot):
        from src.ui.widgets.accounts_model import AccountsTableModel
        from src.ui.widgets.account_list_panel import AccountListPanel
        am = AccountsTableModel()
        am.update_batch([("L", "U1", "NetLiquidation", "1000", "USD")])
        p = AccountListPanel(am)
        qtbot.addWidget(p)
        row = p._account_rows["U1"]
        assert row.set_name("alpha") is True
        assert row.set_name("alpha") is False
