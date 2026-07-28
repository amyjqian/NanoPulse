"""Options Income ticket — end-to-end UI flow against a FakeWorker.

Covers: chain → expiries → strikes → quotes population, default
selections, qty caps (CC shares / CSP buying power), the three synced
price inputs, ineligible states, stale-token rejection and the submitted
payload shape.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from PySide6.QtCore import QObject, Signal

from src.ui.widgets.options_income import OptionsIncomeTicket
from src.ui.widgets.options_income.parts import DEFAULT_CHIPS, SymbolChipBar


class FakeWorker(QObject):
    option_chain_results = Signal(int, dict)
    option_chain_failed = Signal(int, str)
    option_contracts_results = Signal(int, list)
    option_contracts_failed = Signal(int, str)
    option_quote_updated = Signal(int, int, dict)
    option_quotes_failed = Signal(int, str)
    symbol_search_results = Signal(int, list)
    symbol_search_failed = Signal(int, str)
    option_order_result = Signal(dict)

    def __init__(self):
        super().__init__()
        self.chain_calls: list[tuple[int, dict]] = []
        self.contract_calls: list[tuple[int, dict]] = []
        self.quote_calls: list[tuple[int, list]] = []
        self.search_calls: list[tuple[int, str]] = []
        self.quote_cancels = 0

    def request_option_chain(self, token, params):
        self.chain_calls.append((token, params))

    def request_option_contracts(self, token, params):
        self.contract_calls.append((token, params))

    def request_option_quotes(self, token, contracts):
        self.quote_calls.append((token, list(contracts)))

    def cancel_option_quotes(self):
        self.quote_cancels += 1

    def request_symbol_search(self, token, pattern):
        self.search_calls.append((token, pattern))


UNDERLYING_CONID = 4815747
SPOT = 182.40
STRIKES = [170.0, 172.5, 175.0, 177.5, 180.0, 182.5, 185.0, 187.5,
           190.0, 192.5, 195.0, 197.5, 200.0]


def _ymd(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y%m%d")


def _chain() -> dict:
    return {
        "symbol": "NVDA",
        "underlying_conid": UNDERLYING_CONID,
        "exchange": "SMART",
        "trading_class": "NVDA",
        "multiplier": "100",
        "expirations": [_ymd(d) for d in (3, 10, 17, 24, 45, 80)],
        "strikes": STRIKES,
    }


def _contracts(right: str) -> list[dict]:
    out = []
    for i, s in enumerate(STRIKES):
        out.append({
            "conid": 9000 + i if right == "C" else 9500 + i,
            "symbol": "NVDA",
            "sec_type": "OPT",
            "exchange": "SMART",
            "currency": "USD",
            "expiry": _ymd(17),
            "strike": s,
            "right": right,
            "local_symbol": f"NVDA {s:g}{right}",
            "multiplier": "100",
            "trading_class": "NVDA",
            "min_tick": 0.01,
        })
    return out


@pytest.fixture(autouse=True)
def _isolate_chip_settings(monkeypatch):
    """Keep the chip bar off the real QSettings during tests."""
    monkeypatch.setattr(SymbolChipBar, "_load_chips",
                        staticmethod(lambda: list(DEFAULT_CHIPS)))
    monkeypatch.setattr(SymbolChipBar, "_save_chips",
                        lambda self: None)


def _make(qtbot, *, shares=300.0, bp=2_523_356.0, symbol="NVDA",
          worker=None):
    worker = worker if worker is not None else FakeWorker()

    def get_position(_acct, sym):
        if sym == symbol and shares:
            return {"position": shares, "avg_cost": 164.20,
                    "con_id": UNDERLYING_CONID, "market_price": SPOT,
                    "sec_type": "STK", "symbol": sym}
        return None

    t = OptionsIncomeTicket(
        account="mgn-jack-02",
        get_worker=lambda a: worker,
        get_buying_power=lambda a: bp,
        get_position=get_position,
        symbol=symbol,
    )
    qtbot.addWidget(t)
    return t, worker


def _deliver_chain(t, w):
    token = w.chain_calls[-1][0]
    w.option_chain_results.emit(token, _chain())


def _deliver_contracts(t, w, right="C"):
    token = w.contract_calls[-1][0]
    w.option_contracts_results.emit(token, _contracts(right))


def _deliver_selected_quote(t, w, bid=3.04, ask=3.16):
    token = w.quote_calls[-1][0]
    conid = t._selected_leg["conid"]
    w.option_quote_updated.emit(token, conid,
                                {"bid": bid, "ask": ask, "last": 3.08,
                                 "close": 0.0, "delta": 0.29})


# ── Flow ─────────────────────────────────────────────────────────────────────

def test_chain_flow_populates_defaults(qtbot):
    t, w = _make(qtbot)
    assert w.chain_calls, "chain requested on open"
    _deliver_chain(t, w)
    # 4 weekly chips + 2 in More; first chip selected by default.
    assert len(t._expiry._chips) == 4
    assert len(t._expiry._more) == 2
    assert t._expiry.selected().dte == 3
    # Contracts requested for calls (CC default).
    assert w.contract_calls[-1][1]["right"] == "C"
    _deliver_contracts(t, w)
    model = t._chain_view.chain_model
    assert model.rowCount() == 6
    assert model.row_data(0)["strike"] == 182.5
    assert model.row_data(0)["tag"] == "OTM"
    # Default selection = 3rd row (≈0.30-delta zone).
    assert t._selected_leg["strike"] == 187.5
    # Quote batch = 6 strikes + the underlying.
    conids = [c["conid"] for c in w.quote_calls[-1][1]]
    assert UNDERLYING_CONID in conids and len(conids) == 7


def test_quote_sets_price_and_figures(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)          # bid 3.04 / ask 3.16 → mid 3.10
    assert t._rail.price() == Decimal("3.10")
    row = t._chain_view.chain_model.row_data(2)
    assert row["credit"] == 310.0
    assert row["delta"] == 0.29
    assert "SELL 1 NVDA" in t._rail._order_line.text()
    assert "187.5 CALL @ 3.10" in t._rail._order_line.text()
    assert t._rail._sell_btn.isEnabled()
    assert t._rail._credit_lbl.text() == "+$310.00"


def test_cc_max_qty_from_shares(qtbot):
    t, w = _make(qtbot, shares=300.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    assert t._max_qty() == 3
    assert t._rail._max_btn.text() == "MAX 3"
    # Stepper clamps at max.
    t._rail._on_max()
    assert t._rail.qty() == 3
    t._rail._step_qty(+1)
    assert t._rail.qty() == 3


def test_cc_ineligible_under_100_shares(qtbot):
    t, w = _make(qtbot, shares=40.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    assert not t._rail._sell_btn.isEnabled()
    assert t._rail._sell_btn.text() == "Not enough shares"


def test_csp_max_qty_from_buying_power(qtbot):
    t, w = _make(qtbot, bp=2_523_356.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    t._cards._select("CSP")
    # Strategy switch re-requests contracts with right=P.
    assert w.contract_calls[-1][1]["right"] == "P"
    _deliver_contracts(t, w, right="P")
    # Default put row = closest below spot (descending list row 2 = 177.5).
    strike = t._selected_leg["strike"]
    assert strike == 177.5
    assert t._max_qty() == int(2_523_356 // (strike * 100))


def test_price_syncs_three_ways(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    rail = t._rail
    # 1. stepper: +1 tick (0.01)
    rail._step_price(+1)
    assert rail.price() == Decimal("3.11")
    # 2. typed entry, rounded on editingFinished
    rail._px_edit.setText("3.2")
    rail._on_price_typed()
    assert rail.price() == Decimal("3.20")
    # 3. anchor chips
    rail._pick_anchor("BID")
    assert rail.price() == Decimal("3.04")
    rail._pick_anchor("ASK")
    assert rail.price() == Decimal("3.16")


def test_stale_tokens_ignored(qtbot):
    t, w = _make(qtbot)
    token = w.chain_calls[-1][0]
    w.option_chain_results.emit(token - 1, _chain())
    assert t._chain is None


def test_submit_payload_shape(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    t._rail._set_qty(2)

    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()

    assert len(payloads) == 1
    p = payloads[0]
    assert p["action"] == "SELL"
    assert p["type"] == "LMT"
    assert p["sec_type"] == "OPT"
    assert p["qty"] == 2
    assert p["limit_price"] == 3.10
    assert p["_target_accounts"] == ["mgn-jack-02"]
    assert p["_what_if_check"] is True
    rc = p["_resolved_contract"]
    assert rc["conid"] == 9007          # 187.5 call
    assert rc["right"] == "C"
    assert rc["strike"] == 187.5
    assert p["profit_take"] is None and p["stop_loss"] is None


def test_close_cancels_quotes(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    before = w.quote_cancels
    t.reject()
    assert w.quote_cancels > before


# ── Model accessor ───────────────────────────────────────────────────────────

def test_position_for_symbol_roundtrip(qtbot):
    from src.ui.widgets.account_position_model import AccountPositionModel
    m = AccountPositionModel()
    m.update_positions([{
        "label": "conn1", "account": "mgn-jack-02", "con_id": UNDERLYING_CONID,
        "symbol": "NVDA", "local_symbol": "NVDA", "sec_type": "STK",
        "exchange": "NASDAQ", "currency": "USD", "multiplier": "",
        "position": 300.0, "avg_cost": 164.20, "market_price": 182.40,
        "market_value": 54720.0, "unrealized_pnl": 5460.0,
        "realized_pnl": 0.0,
    }])
    got = m.position_for_symbol("mgn-jack-02", "NVDA")
    assert got is not None and got["position"] == 300.0
    assert m.position_for_symbol("mgn-jack-02", "TSLA") is None
    assert m.position_data("mgn-jack-02", UNDERLYING_CONID)["symbol"] == "NVDA"
