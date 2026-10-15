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
from collections import defaultdict
from types import SimpleNamespace

import pytest

from src.core.ib_worker import IBWorkerThread
from src.ui.widgets.dom_ladder import DOMLadderModel
import src.ui.widgets.book_trader_view as btv


# ── fakes ──────────────────────────────────────────────────────────────────────

class _FakeTicker:
    """Hashable by identity, like ib_async's Ticker."""

    def __init__(self, contract):
        self.contract = contract
        self.domBids: list = []
        self.domAsks: list = []
        self.ticks: list = []


class _FakeIB:
    """ib_async keeps ONE Ticker per contract: L1 and depth requests for
    the same conId share it, told apart only by their reqIds."""

    def __init__(self):
        self.depth_calls: list[tuple[int, str, int, bool]] = []
        self.cancel_calls: list[tuple[int, bool]] = []
        self.mkt_calls: list[int] = []
        self.mkt_cancels: list[int] = []
        self.wrapper = SimpleNamespace(
            reqId2Ticker={}, ticker2ReqId=defaultdict(dict))
        self._tickers: dict[int, _FakeTicker] = {}
        self._next_req = 100

    def isConnected(self):
        return True

    def _start(self, contract, tick_type):
        ticker = self._tickers.setdefault(
            contract.conId, _FakeTicker(contract))
        self.wrapper.reqId2Ticker[self._next_req] = ticker
        self.wrapper.ticker2ReqId[tick_type][ticker] = self._next_req
        self._next_req += 1
        return ticker

    def reqMktDepth(self, contract, numRows=5, isSmartDepth=False):
        self.depth_calls.append(
            (contract.conId, contract.exchange, numRows, isSmartDepth))
        return self._start(contract, "mktDepth")

    def cancelMktDepth(self, contract, isSmartDepth=False):
        self.cancel_calls.append((contract.conId, isSmartDepth))

    def reqMarketDataType(self, _type):
        pass

    def reqMktData(self, contract, genericTickList="", snapshot=False):
        self.mkt_calls.append(contract.conId)
        return self._start(contract, "mktData")

    def cancelMktData(self, contract):
        self.mkt_cancels.append(contract.conId)


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


# ── worker: refused quote stream is reported and re-requested ─────────────────

class TestQuoteRefused:
    """TWS can drop a free venue (IBEOS) from the account's permissions at
    a session change and route a stock's quote to an unsubscribed
    exchange — error 10089, and the ladder sits around 0.00.  The worker
    reports it once, re-requests on a backoff, and clears it on the first
    real price tick."""

    _ERR = ("Requested market data requires additional subscription for "
            "API.NVDA NASDAQ.NMS/TOP/ALL")

    def _subscribe(self, worker):
        _run(worker._do_subscribe_bg_symbols([_NVDA]))
        ticker = worker._bg_tickers["NVDA"]
        req_id = worker._ib.wrapper.ticker2ReqId["mktData"][ticker]
        return req_id, ticker

    def test_10089_reports_once(self, worker, qtbot):
        req_id, ticker = self._subscribe(worker)
        seen = []
        worker.quote_status_changed.connect(lambda s, ok: seen.append((s, ok)))
        worker._on_ib_error(req_id, 10089, self._ERR, ticker.contract)
        worker._on_ib_error(req_id, 10089, self._ERR, ticker.contract)
        assert seen == [("NVDA", False)]
        assert "NVDA" in worker._l1_refused

    def test_l1_refusal_leaves_the_depth_stream_alone(self, worker, qtbot):
        _run(worker._do_subscribe_depth("NVDA", 10))
        req_id, ticker = self._subscribe(worker)   # same Ticker, L1 reqId
        with qtbot.assertNotEmitted(worker.market_data_failed):
            worker._on_ib_error(req_id, 354, "not subscribed", ticker.contract)
        assert "NVDA" in worker._depth_tickers
        assert "NVDA" in worker._l1_refused

    def test_depth_refusal_is_not_a_quote_refusal(self, worker, qtbot):
        _run(worker._do_subscribe_depth("NVDA", 10))
        _ticker_req, ticker = self._subscribe(worker)
        depth_req = worker._ib.wrapper.ticker2ReqId["mktDepth"][ticker]
        with qtbot.assertNotEmitted(worker.quote_status_changed):
            worker._on_ib_error(depth_req, 354, "not subscribed",
                                ticker.contract)
        assert worker._l1_refused == {}

    def test_retry_resubscribes_when_due_with_backoff(self, worker,
                                                      monkeypatch):
        import src.core.ib_worker as ibw
        req_id, ticker = self._subscribe(worker)
        worker._on_ib_error(req_id, 10089, self._ERR, ticker.contract)
        clock = [1000.0]
        monkeypatch.setattr(ibw.time, "monotonic", lambda: clock[0])
        worker._l1_refused["NVDA"] = (0, 1010.0)
        worker._retry_refused_quotes()              # not due yet
        assert worker._ib.mkt_calls == [4815747]
        clock[0] = 1010.0
        worker._retry_refused_quotes()
        assert worker._ib.mkt_cancels == [4815747]
        assert worker._ib.mkt_calls == [4815747, 4815747]
        assert worker._l1_refused["NVDA"] == (1, 1010.0 + 60.0)
        clock[0] = 5000.0
        for _ in range(6):
            worker._retry_refused_quotes()
            clock[0] += 1000.0
        assert worker._l1_refused["NVDA"][1] - (clock[0] - 1000.0) \
            == ibw._L1_RETRY_MAX_S

    def test_first_real_price_tick_clears_it(self, worker, qtbot):
        req_id, ticker = self._subscribe(worker)
        worker._on_ib_error(req_id, 10089, self._ERR, ticker.contract)
        # A depth tick on the shared Ticker is not a quote.
        ticker.ticks = [SimpleNamespace(tickType=99, price=217.0)]
        worker._on_pending_tickers([ticker])
        assert "NVDA" in worker._l1_refused
        ticker.ticks = [SimpleNamespace(tickType=4, price=217.0)]
        with qtbot.waitSignal(worker.quote_status_changed,
                              timeout=1000) as sig:
            worker._on_pending_tickers([ticker])
        assert sig.args == ["NVDA", True]
        assert worker._l1_refused == {}

    def test_unsubscribe_drops_the_refusal(self, worker):
        req_id, ticker = self._subscribe(worker)
        worker._on_ib_error(req_id, 10089, self._ERR, ticker.contract)
        _run(worker._do_unsubscribe_bg_symbol("NVDA"))
        assert worker._l1_refused == {}


class TestQuoteNotice:
    def test_refusal_shows_notice_and_clears(self, view):
        view.on_quote_status_changed("NVDA", False)
        assert "No market data" in view._ticker.notice
        view.on_quote_status_changed("NVDA", True)
        assert view._ticker.notice == ""

    def test_other_symbol_does_not_touch_the_strip(self, view):
        view.on_quote_status_changed("AAPL", False)
        assert view._ticker.notice == ""

    def test_refused_ladder_centres_on_last_known_price(self, view):
        view._bg_price_cache["NVDA"] = (0.0, 0.0, 0.0, 221.30)  # close only
        view.on_quote_status_changed("NVDA", False)
        prices = view._model.prices
        assert prices[len(prices) // 2] == pytest.approx(221.30)
