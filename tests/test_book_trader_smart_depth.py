"""Stock depth on the Book Trader ladder.

SMART-routed contracts only have depth as *smart depth*: TWS aggregates
the venues the account is entitled to and answers a plain per-exchange
request with error 10092.  Futures on a direct exchange take the plain
form.  The worker must pick the flag from the contract's exchange, tell
``cancelMktDepth`` the same flag, and report a refused depth request to
the ladder so it stops waiting for a book.

Smart depth also lists one level per venue, so the ladder model sums
levels that land on the same price, and because TWS can leave a stock's
L1 stream silent (overnight-only entitlement) the view centres the
ladder on the book's mid when no last price has arrived.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.core.ib_worker import IBWorkerThread
from src.ui.widgets.dom_ladder import DOMLadderModel
import src.ui.widgets.book_trader_view as btv


# ── fakes ──────────────────────────────────────────────────────────────────────

class _FakeIB:
    def __init__(self):
        self.depth_calls: list[tuple[int, str, int, bool]] = []
        self.cancel_calls: list[tuple[int, bool]] = []
        self.wrapper = SimpleNamespace(reqId2Ticker={})
        self._next_req = 100

    def isConnected(self):
        return True

    def reqMktDepth(self, contract, numRows=5, isSmartDepth=False):
        self.depth_calls.append(
            (contract.conId, contract.exchange, numRows, isSmartDepth))
        ticker = SimpleNamespace(contract=contract, domBids=[], domAsks=[])
        self.wrapper.reqId2Ticker[self._next_req] = ticker
        self._next_req += 1
        return ticker

    def cancelMktDepth(self, contract, isSmartDepth=False):
        self.cancel_calls.append((contract.conId, isSmartDepth))


_NVDA = {"conid": "4815747", "symbol": "NVDA", "sec_type": "STK",
         "exchange": "SMART", "currency": "USD", "local_symbol": "NVDA"}
_MNQ = {"conid": "815824267", "symbol": "MNQ", "sec_type": "FUT",
        "exchange": "CME", "currency": "USD", "local_symbol": "MNQZ6",
        "expiry": "20261218", "multiplier": "2"}


@pytest.fixture
def worker(qapp, monkeypatch):
    import src.core.instruments as instruments
    book = {"NVDA": _NVDA, "MNQ": _MNQ}
    monkeypatch.setattr(instruments, "get_contract",
                        lambda sym: book.get(sym.upper()))
    wt = IBWorkerThread("test")
    wt._ib = _FakeIB()
    return wt


def _run(coro):
    return asyncio.run(coro)


# ── worker: request / cancel flags ────────────────────────────────────────────

class TestSmartDepthFlag:
    def test_smart_routed_stock_requests_smart_depth(self, worker):
        _run(worker._do_subscribe_depth("NVDA", 10))
        assert worker._ib.depth_calls == [(4815747, "SMART", 10, True)]
        assert worker._depth_smart["NVDA"] is True

    def test_direct_routed_future_requests_plain_depth(self, worker):
        _run(worker._do_subscribe_depth("MNQ", 10))
        assert worker._ib.depth_calls == [(815824267, "CME", 10, False)]
        assert worker._depth_smart["MNQ"] is False

    def test_cancel_repeats_the_request_flag(self, worker):
        _run(worker._do_subscribe_depth("NVDA", 10))
        _run(worker._do_subscribe_depth("MNQ", 10))
        _run(worker._do_unsubscribe_depth("NVDA"))
        _run(worker._do_unsubscribe_all_depth())
        assert sorted(worker._ib.cancel_calls) == [
            (4815747, True), (815824267, False)]
        assert worker._depth_smart == {}
        assert worker._depth_tickers == {}


# ── worker: refused depth reaches the ladder ──────────────────────────────────

class TestDepthRefused:
    def _subscribe_nvda(self, worker):
        _run(worker._do_subscribe_depth("NVDA", 10))
        (req_id, ticker), = worker._ib.wrapper.reqId2Ticker.items()
        return req_id, ticker

    def test_10092_emits_market_data_failed_and_frees_the_symbol(
            self, worker, qtbot):
        req_id, ticker = self._subscribe_nvda(worker)
        with qtbot.waitSignal(worker.market_data_failed, timeout=1000) as sig:
            worker._on_ib_error(
                req_id, 10092,
                "Deep market data is not supported for this combination "
                "of security type/exchange", ticker.contract)
        assert sig.args == ["NVDA"]
        # The dead ticker is gone, so the next subscribe is not skipped
        # as "already subscribed".
        assert "NVDA" not in worker._depth_tickers
        _run(worker._do_subscribe_depth("NVDA", 10))
        assert len(worker._ib.depth_calls) == 2

    def test_354_on_depth_request_emits_too(self, worker, qtbot):
        req_id, ticker = self._subscribe_nvda(worker)
        with qtbot.waitSignal(worker.market_data_failed, timeout=1000):
            worker._on_ib_error(req_id, 354,
                                "Requested market data is not subscribed.",
                                ticker.contract)

    def test_354_on_an_unrelated_request_is_ignored(self, worker, qtbot):
        req_id, ticker = self._subscribe_nvda(worker)
        # Same contract, different request (the L1 stream) → not ours.
        with qtbot.assertNotEmitted(worker.market_data_failed):
            worker._on_ib_error(req_id + 1, 354,
                                "Requested market data is not subscribed.",
                                ticker.contract)
        assert "NVDA" in worker._depth_tickers


# ── model: one row per price even when venues repeat a price ─────────────────

class TestLevelsSumPerPrice:
    def test_duplicate_prices_sum(self, qapp):
        m = DOMLadderModel()
        m.set_tick_size(0.01)
        m.recenter(218.47)
        m.feed_depth(
            [(218.42, 55), (218.42, 179), (218.40, 100)],   # IBEOS + OVERNIGHT
            [(218.52, 55), (218.56, 218)])
        assert m._bid[218.42] == 234
        assert m._bid[218.40] == 100
        assert m._max_size == 234


# ── view: silent L1, book arrives ─────────────────────────────────────────────

@pytest.fixture
def view(qtbot, monkeypatch):
    import src.core.instruments as instruments
    monkeypatch.setattr(instruments, "get_contract",
                        lambda sym: _NVDA if sym.upper() == "NVDA" else None)
    v = btv.BookTraderView(active_threads={})
    qtbot.addWidget(v)
    v._active_symbol = "NVDA"
    v._model.set_tick_size(0.01)
    v._model.recenter(0.0)
    return v


class TestWarmStartFromDepth:
    def test_book_without_last_centres_ladder_and_fills_header(self, view):
        assert view._model.prices[len(view._model.prices) // 2] == 0.0
        view.feed_depth("NVDA", [(218.42, 55)], [(218.52, 55)])
        prices = view._model.prices
        assert prices[len(prices) // 2] == pytest.approx(218.47)
        assert view._model._bid[218.42] == 55
        assert view._ticker._bid == pytest.approx(218.42)
        assert view._ticker._ask == pytest.approx(218.52)
        assert view._l2_seen is True

    def test_later_books_do_not_recentre_again(self, view):
        view.feed_depth("NVDA", [(218.42, 55)], [(218.52, 55)])
        view.feed_depth("NVDA", [(219.90, 10)], [(220.10, 10)])
        prices = view._model.prices
        assert prices[len(prices) // 2] == pytest.approx(218.47)

    def test_real_last_price_keeps_control(self, view):
        view.feed_price("NVDA", 230.0, 229.9, 230.1)
        view.feed_depth("NVDA", [(218.42, 55)], [(218.52, 55)])
        prices = view._model.prices
        assert prices[len(prices) // 2] == pytest.approx(230.0)
        assert view._ticker._bid == pytest.approx(229.9)

    def test_other_symbol_is_ignored(self, view):
        view.feed_depth("MNQ", [(29800.0, 5)], [(29800.25, 5)])
        assert view._model.prices[len(view._model.prices) // 2] == 0.0
