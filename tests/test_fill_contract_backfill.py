"""Tests for the conId→metadata fill backfill in ib_worker.IBWorkerThread.

Option orders are placed on a lean conId-only contract (TWS error 478
workaround), and ib_async attaches that same lean ``trade.contract`` to
every live Fill — so ``_flatten_fill`` used to emit fills with a blank
symbol/secType and multiplier 1.0, which the Trades view rendered as a
"—" row with no Type and option notionals understated 100×.

``_do_place_order`` now records the placing params' contract metadata in
``_conid_contract_meta`` and ``_flatten_fill`` backfills blank contract
fields from it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.ib_worker import IBWorkerThread


_UTC_TIME = datetime(2026, 8, 19, 15, 33, 57, tzinfo=timezone.utc)


def _make_worker() -> IBWorkerThread:
    """Bare IBWorkerThread bypassing the QThread `__init__`.

    ``_flatten_fill`` only touches ``self._label`` and
    ``self._conid_contract_meta``.
    """
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._label = "test"
    wt._conid_contract_meta = {}
    return wt


def _fill(contract) -> SimpleNamespace:
    execution = SimpleNamespace(
        execId="0001eb2e.6a85b049.01.01",
        time=_UTC_TIME,
        side="SLD",
        shares=1.0,
        price=0.8,
        exchange="CBOE",
        orderRef="nano_260819_113355_abcd",
        acctNumber="DU111",
        orderId=7,
        clientId=1,
        liquidation=0,
        lastLiquidity=1,
    )
    # ib_async attaches a default-initialised CommissionReport (execId="").
    report = SimpleNamespace(execId="", commission=0.0, realizedPNL=0.0)
    return SimpleNamespace(
        contract=contract, execution=execution, commissionReport=report)


def _lean_contract() -> SimpleNamespace:
    """What trade.contract looks like for an option order placed with the
    conId-only 478 workaround."""
    return SimpleNamespace(
        conId=712345678, symbol="", localSymbol="", secType="",
        multiplier="", lastTradeDateOrContractMonth="", strike=0.0,
        right="")


_OPT_META = {
    "symbol": "AAPL",
    "local_symbol": "AAPL  260918C00250000",
    "sec_type": "OPT",
    "expiry": "20260918",
    "multiplier": "100",
    "strike": 250.0,
    "right": "C",
}


class TestFillContractBackfill:
    def test_lean_option_fill_is_backfilled(self):
        wt = _make_worker()
        wt._conid_contract_meta[712345678] = dict(_OPT_META)

        d = wt._flatten_fill(_fill(_lean_contract()))

        assert d["symbol"] == "AAPL"
        assert d["local_symbol"] == "AAPL  260918C00250000"
        assert d["sec_type"] == "OPT"
        assert d["multiplier"] == 100.0
        assert d["expiry"] == "20260918"
        assert d["strike"] == 250.0
        assert d["right"] == "C"
        # Execution fields are untouched by the backfill.
        assert d["side"] == "SLD"
        assert d["price"] == 0.8
        assert d["con_id"] == 712345678

    def test_lean_fill_without_meta_stays_blank(self):
        wt = _make_worker()
        d = wt._flatten_fill(_fill(_lean_contract()))
        assert d["symbol"] == ""
        assert d["local_symbol"] == ""
        assert d["sec_type"] == ""
        assert d["multiplier"] == 1.0

    def test_populated_contract_is_not_overwritten(self):
        wt = _make_worker()
        # Stale/mismatched meta for the same conId must not clobber a
        # fill whose contract arrived fully populated (snapshot path).
        wt._conid_contract_meta[712345678] = dict(
            _OPT_META, symbol="WRONG", multiplier="7")
        contract = SimpleNamespace(
            conId=712345678, symbol="AAPL",
            localSymbol="AAPL  260918C00250000", secType="OPT",
            multiplier="100", lastTradeDateOrContractMonth="20260918",
            strike=250.0, right="C")

        d = wt._flatten_fill(_fill(contract))

        assert d["symbol"] == "AAPL"
        assert d["multiplier"] == 100.0

    def test_blank_meta_multiplier_keeps_fallback(self):
        # A chain contract may omit the multiplier — the flattened fill
        # keeps the 1.0 fallback and the summary model's OPT→100 default
        # takes over downstream.
        wt = _make_worker()
        wt._conid_contract_meta[712345678] = dict(_OPT_META, multiplier="")
        d = wt._flatten_fill(_fill(_lean_contract()))
        assert d["sec_type"] == "OPT"
        assert d["multiplier"] == 1.0
