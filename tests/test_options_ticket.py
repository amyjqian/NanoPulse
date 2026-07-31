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
    order_status_updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self.chain_calls: list[tuple[int, dict]] = []
        self.contract_calls: list[tuple[int, dict]] = []
        self.quote_calls: list[tuple[int, list]] = []
        self.search_calls: list[tuple[int, str]] = []
        self.cancel_order_calls: list[str] = []
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

    def cancel_order(self, order_id):
        self.cancel_order_calls.append(order_id)


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
    """Keep the chip bar off the real QSettings and the ticket geometry
    off the real config JSON during tests."""
    monkeypatch.setattr(SymbolChipBar, "_load_chips",
                        staticmethod(lambda: list(DEFAULT_CHIPS)))
    monkeypatch.setattr(SymbolChipBar, "_save_chips",
                        lambda self: None)
    from src.core import config_manager
    monkeypatch.setattr(config_manager, "load_layout",
                        lambda path=None: {})
    monkeypatch.setattr(config_manager, "save_layout",
                        lambda layout, path=None: None)


def _make(qtbot, *, shares=300.0, bp=2_523_356.0, symbol="NVDA",
          is_margin=True, worker=None):
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
        is_margin=is_margin,
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

def test_account_level_open_selects_no_chip(qtbot):
    # Right-click at account level → no symbol → nothing pre-selected,
    # no chain requested until the user picks a chip.
    t, w = _make(qtbot, symbol=None)
    assert t._chips.active_symbol() == ""
    assert not w.chain_calls
    # Placeholder expiries: the next four calendar Fridays still show.
    assert len(t._expiry._chips) == 4
    from datetime import datetime as _dt
    assert all(_dt.strptime(e.ymd, "%Y%m%d").weekday() == 4
               for e in t._expiry._chips)
    t._chips.select("NVDA")
    assert w.chain_calls


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
    # 13 strikes (10-OTM + 5-ITM windows cover the whole test chain)
    # plus the ATM separator row between the OTM and ITM blocks.
    assert model.rowCount() == 14
    seps = [i for i in range(model.rowCount())
            if model.row_data(i).get("sep")]
    assert seps == [8]                     # 182.5 above, 180 below
    assert model.row_data(8)["label"] == "NVDA 182.40"
    # Descending — highest strike on top.
    strikes = [model.row_data(i)["strike"] for i in range(model.rowCount())
               if not model.row_data(i).get("sep")]
    assert strikes == sorted(strikes, reverse=True)
    assert strikes[0] == 200.0 and strikes[-1] == 170.0
    # Default selection ≈ two strikes OTM of the money (anchor 182.5,
    # minus two on the descending ladder → 187.5).
    assert t._selected_leg["strike"] == 187.5
    # Quote batch = 13 strikes + the underlying (no separator).
    conids = [c["conid"] for c in w.quote_calls[-1][1]]
    assert UNDERLYING_CONID in conids and len(conids) == 14


def test_symbol_change_resets_expiry_to_nearest_week(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    # User picks a later weekly…
    t._expiry._pick(t._expiry._chips[2])
    assert t._expiry.selected().dte == 17
    # …then switches symbol: the new chain defaults back to the
    # nearest weekly even though the same expiry date is listed.
    t._chips.select("TSLA")
    _deliver_chain(t, w)
    assert t._expiry.selected().dte == 3


def test_sep_row_not_selectable_and_tracks_spot(qtbot):
    from PySide6.QtCore import Qt
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    model = t._chain_view.chain_model
    # Separator carries no flags → mouse/keyboard can't land on it.
    assert model.flags(model.index(8, 0)) == Qt.ItemFlag.NoItemFlags
    # select_row(sep) snaps to the nearest strike row instead.
    t._chain_view.select_row(8)
    assert t._selected_leg["strike"] in (182.5, 180.0)
    # Underlying quote moves the spot → separator label follows.
    token = w.quote_calls[-1][0]
    w.option_quote_updated.emit(token, UNDERLYING_CONID,
                                {"bid": 183.0, "ask": 183.2, "last": 183.1,
                                 "close": 181.0, "delta": 0.0})
    assert model.row_data(8)["label"] == "NVDA 183.10"


def test_large_chain_shows_50_per_side_but_windows_quotes(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    token = w.contract_calls[-1][0]
    big = []
    for i in range(1, 161):                            # strikes 2.5 … 400
        s = 2.5 * i
        big.append({"conid": 20000 + i, "symbol": "NVDA", "sec_type": "OPT",
                    "exchange": "SMART", "currency": "USD",
                    "expiry": _ymd(17), "strike": s, "right": "C",
                    "local_symbol": f"NVDA {s:g}C", "multiplier": "100",
                    "trading_class": "NVDA", "min_tick": 0.01})
    w.option_contracts_results.emit(token, big)
    model = t._chain_view.chain_model
    assert model.rowCount() == 101                     # 50+50 strikes + sep
    # Quote batch is windowed around the selection, not the whole chain
    # (IB market-data lines are a shared, limited resource).
    subs = w.quote_calls[-1][1]
    conids = [c["conid"] for c in subs]
    assert UNDERLYING_CONID in conids
    assert t._selected_leg["conid"] in conids
    assert len(subs) <= 2 * t._QUOTE_PAD + 2


def test_quote_sets_price_and_figures(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)          # bid 3.04 / ask 3.16 → mid 3.10
    assert t._rail.price() == Decimal("3.10")
    model = t._chain_view.chain_model
    row = model.row_data(model.row_for_conid(t._selected_leg["conid"]))
    assert row["credit"] == 310.0
    assert row["delta"] == 0.29
    assert (row["bid"], row["ask"], row["last"]) == (3.04, 3.16, 3.08)
    assert t._rail._order_line.text().startswith("NVDA ")
    assert t._rail._order_line.text().endswith("187.5 CALL")
    assert t._rail._sell_btn.isEnabled()


def test_cc_max_qty_from_shares(qtbot):
    # Cash account → covered only: MAX bound by shares.
    t, w = _make(qtbot, shares=300.0, is_margin=False)
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
    # Cash account with <100 shares cannot sell calls at all.
    t, w = _make(qtbot, shares=40.0, is_margin=False)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    assert not t._rail._sell_btn.isEnabled()
    assert t._rail._sell_btn.text() == "Not enough shares"


def test_margin_account_can_sell_naked_calls(qtbot):
    # Margin account, no shares → sell call still eligible; MAX comes
    # from the ~20%-of-underlying margin estimate.
    t, w = _make(qtbot, shares=0.0, bp=100_000.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    # No position → the spot arrives via the underlying quote stream.
    token = w.quote_calls[-1][0]
    w.option_quote_updated.emit(token, UNDERLYING_CONID,
                                {"bid": 182.3, "ask": 182.5, "last": 182.40,
                                 "close": 181.0, "delta": 0.0})
    # naked cap = 100,000 // (0.20 × 182.40 × 100) = 27
    assert t._max_qty() == 27
    _deliver_selected_quote(t, w)
    assert t._rail._sell_btn.isEnabled()


def test_cash_account_cannot_sell_puts(qtbot):
    t, w = _make(qtbot, is_margin=False)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    t._right_toggle._pick("P")
    _deliver_contracts(t, w, right="P")
    _deliver_selected_quote(t, w)
    assert not t._rail._sell_btn.isEnabled()
    assert t._rail._sell_btn.text() == "Not enough buying power"
    # Buying the same put is fine on a cash account.
    t._rail._pick_side("BUY")
    _deliver_contracts(t, w, right="P")
    _deliver_selected_quote(t, w)
    assert t._rail._sell_btn.isEnabled()


def test_csp_max_qty_from_buying_power(qtbot):
    t, w = _make(qtbot, bp=2_523_356.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    t._right_toggle._pick("P")
    # Right switch re-requests contracts with right=P.
    assert w.contract_calls[-1][1]["right"] == "P"
    _deliver_contracts(t, w, right="P")
    # Default put ≈ two strikes OTM below the money.
    strike = t._selected_leg["strike"]
    assert strike == 175.0
    assert t._max_qty() == int(2_523_356 // (strike * 100))


def test_buy_call_flow(qtbot):
    t, w = _make(qtbot, bp=10_000.0)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    t._rail._pick_side("BUY")
    assert w.contract_calls[-1][1]["right"] == "C"
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)          # mid 3.10
    # Long-side cap = BP // (premium × 100) = 10,000 // 310 = 32.
    assert t._max_qty() == 32
    assert t._rail._sell_btn.text() == "Submit order"
    # Premium yield is an income metric — blank on long strategies.
    model = t._chain_view.chain_model
    row = model.row_for_conid(t._selected_leg["conid"])
    assert model.row_data(row)["yield_pct"] is None

    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()
    assert len(payloads) == 1
    p = payloads[0]
    assert p["action"] == "BUY"
    assert p["type"] == "LMT"
    assert p["limit_price"] == 3.10
    assert p["_resolved_contract"]["right"] == "C"


def test_side_switch_reuses_loaded_strikes(qtbot):
    # Buy ↔ Sell keeps the same (expiry, right) contracts — no re-fetch,
    # no "Loading strikes…", selection and price preserved.
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)          # mid 3.10
    n_calls = len(w.contract_calls)
    leg = t._selected_leg
    model = t._chain_view.chain_model
    row = model.row_for_conid(leg["conid"])

    t._rail._pick_side("BUY")
    assert len(w.contract_calls) == n_calls
    assert t._selected_leg is leg
    assert t._rail.price() == Decimal("3.10")
    assert model.row_data(row)["yield_pct"] is None      # blank for longs

    t._rail._pick_side("SELL")
    assert len(w.contract_calls) == n_calls
    # Yield re-derived from the cached mid: 3.10 / 182.40 × 100.
    assert model.row_data(row)["yield_pct"] == 1.70


def test_buy_put_requests_puts(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    t._rail._pick_side("BUY")
    t._right_toggle._pick("P")
    assert w.contract_calls[-1][1]["right"] == "P"
    _deliver_contracts(t, w, right="P")
    _deliver_selected_quote(t, w)
    assert t._rail._sell_btn.text() == "Submit order"
    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()
    assert payloads and payloads[0]["action"] == "BUY"
    assert payloads[0]["_resolved_contract"]["right"] == "P"


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


def test_delta_keeps_last_known_value(qtbot):
    # A later computation tick with an empty delta must NOT blank a row's
    # previously-known delta (IB sends partial greek computations).
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)          # delta 0.29
    token = w.quote_calls[-1][0]
    conid = t._selected_leg["conid"]
    w.option_quote_updated.emit(token, conid,
                                {"bid": 3.05, "ask": 3.15, "last": 3.10,
                                 "close": 0.0, "delta": 0.0})
    model = t._chain_view.chain_model
    row = model.row_data(model.row_for_conid(conid))
    assert row["delta"] == 0.29            # kept, not blanked
    assert row["bid"] == 3.05              # quote fields still updated


def test_delta_falls_back_to_bs_estimate(qtbot):
    # No IB greek computation for the leg → delta estimated from the mid
    # premium via Black-Scholes; a real IB delta overrides it later.
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    token = w.quote_calls[-1][0]
    conid = t._selected_leg["conid"]
    w.option_quote_updated.emit(token, conid,
                                {"bid": 3.04, "ask": 3.16, "last": 3.10,
                                 "close": 0.0, "delta": 0.0})
    model = t._chain_view.chain_model
    row = model.row_data(model.row_for_conid(conid))
    assert 0.05 < row["delta"] < 0.7        # plausible OTM-call estimate
    assert not row.get("delta_ib")
    # IB's own computation wins once it arrives.
    w.option_quote_updated.emit(token, conid,
                                {"bid": 3.04, "ask": 3.16, "last": 3.10,
                                 "close": 0.0, "delta": 0.29})
    assert row["delta"] == 0.29
    assert row["delta_ib"]


def test_zero_bid_shown_and_missing_fields_keep_values(qtbot):
    from src.ui.widgets.options_income.chain_table import COL_BID, COL_LAST
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    token = w.quote_calls[-1][0]
    conid = t._selected_leg["conid"]
    model = t._chain_view.chain_model
    row = model.row_for_conid(conid)
    # A real zero bid (deep OTM, no buyers) renders 0.00; a field IB
    # never sent (None) stays "—".
    w.option_quote_updated.emit(token, conid,
                                {"bid": 0.0, "ask": 0.05, "last": None,
                                 "close": None, "delta": 0.0})
    assert model.data(model.index(row, COL_BID)) == "0.00"
    assert model.data(model.index(row, COL_LAST)) == "—"
    # A later tick missing bid/ask must not blank the known values.
    w.option_quote_updated.emit(token, conid,
                                {"bid": None, "ask": None, "last": 0.04,
                                 "close": None, "delta": 0.0})
    rd = model.row_data(row)
    assert (rd["bid"], rd["ask"], rd["last"]) == (0.0, 0.05, 0.04)


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
    assert rc["conid"] == 9007          # 187.5 call (default leg)
    assert rc["right"] == "C"
    assert rc["strike"] == 187.5
    assert p["profit_take"] is None and p["stop_loss"] is None


def test_market_order_submit(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    rail = t._rail
    rail._pick_anchor("MKT")
    # Market mode: price input greys out, Mkt chip active, sell enabled.
    assert rail.is_market()
    assert not rail._px_edit.isEnabled()
    assert rail._sell_btn.isEnabled()

    payloads = []
    t.order_ready.connect(payloads.append)
    rail._sell_btn.click()
    assert len(payloads) == 1
    p = payloads[0]
    assert p["type"] == "MKT"
    assert p["limit_price"] == 0.0
    assert p["last_price"] == 3.10          # mid rides along as estimate
    # Picking a price anchor drops back to a limit order.
    rail._pick_anchor("BID")
    assert not rail.is_market()
    assert rail._px_edit.isEnabled()


def _status_event(ref, status, *, account="mgn-jack-02", fill="–", err=""):
    return {"order_id": f"{account}_{ref}", "order_ref": ref,
            "symbol": "NVDA 187.5C", "account": account, "action": "SELL",
            "order_type": "LMT", "quantity": "2", "limit_price": "3.10",
            "stop_price": "–", "tif": "DAY", "fill_price": fill,
            "status": status, "time": "10:30:45",
            "time_sort": "2026-01-01T10:30:45", "error_message": err}


def test_submit_keeps_ticket_open_and_tracks_session(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    t._rail._set_qty(2)

    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()

    p = payloads[0]
    # The ticket pre-generates a worker-format orderRef for tracking.
    assert p["order_ref"].startswith("nano_")
    # Dialog is still up (not accepted) and ready for the next order.
    assert t.result() == 0
    assert not t._session.isHidden()
    assert p["order_ref"] in t._session._rows
    # Qty snaps back to 1; submit is locked out briefly; Cancel → Close.
    assert t._rail.qty() == 1
    assert not t._rail._sell_btn.isEnabled()
    assert t._rail._sell_btn.text() == "Order submitted ✓"
    assert t._rail._cancel_btn.text() == "Close"
    # The relock timer re-enables through the normal recompute path.
    t._submit_relock.stop()
    t._recompute()
    assert t._rail._sell_btn.isEnabled()


def test_session_strip_status_flow_and_cancel(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)

    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()
    ref = payloads[0]["order_ref"]
    assert ref in t._session._rows

    # Pre-check → open: Cancel appears only once the order is at TWS.
    w.order_status_updated.emit(_status_event(ref, "Margin check"))
    assert not t._session.can_cancel(ref)
    w.order_status_updated.emit(_status_event(ref, "Submitted"))
    assert t._session.can_cancel(ref)
    assert t._session.status_text(ref) == "Submitted"

    # Cancel click → worker.cancel_order with the blotter key.
    t._session.request_cancel(ref)
    assert w.cancel_order_calls == [f"mgn-jack-02_{ref}"]
    assert not t._session.can_cancel(ref)       # double-click guard
    t._session.request_cancel(ref)              # no second worker call
    assert len(w.cancel_order_calls) == 1
    w.order_status_updated.emit(_status_event(ref, "Cancelled"))
    assert not t._session.can_cancel(ref)
    assert t._session.status_text(ref) == "Cancelled"

    # Statuses for other windows' orders are ignored.
    w.order_status_updated.emit(_status_event("nano_0731_093000_zzzz",
                                              "Submitted"))
    assert "nano_0731_093000_zzzz" not in t._session._rows


def test_cancel_requires_confirm_click(qtbot):
    from src.ui.widgets.options_income.session_strip import COL_CANCEL
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)
    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()
    ref = payloads[0]["order_ref"]
    w.order_status_updated.emit(_status_event(ref, "Submitted"))

    strip = t._session
    idx = strip._model.index(0, COL_CANCEL)
    # First click only ARMS — nothing sent to the worker.
    strip._on_clicked(idx)
    assert w.cancel_order_calls == []
    assert strip._rows[ref]["confirm"] is True
    assert strip._confirm_timer.isActive()
    # Second click confirms → cancel goes out.
    strip._on_clicked(idx)
    assert w.cancel_order_calls == [f"mgn-jack-02_{ref}"]
    assert strip._rows[ref]["confirm"] is False

    # Re-arm after the order re-opens, then let the window lapse.
    w.order_status_updated.emit(_status_event(ref, "Submitted"))
    strip._on_clicked(idx)
    assert strip._rows[ref]["confirm"] is True
    strip._disarm_confirm()                    # timer timeout path
    assert strip._rows[ref]["confirm"] is False
    assert len(w.cancel_order_calls) == 1      # nothing extra sent
    # A status event under an armed confirm also disarms.
    strip._on_clicked(idx)
    w.order_status_updated.emit(_status_event(ref, "Submitted"))
    assert strip._rows[ref]["confirm"] is False


def test_session_strip_fill_and_reject(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    _deliver_selected_quote(t, w)

    payloads = []
    t.order_ready.connect(payloads.append)
    t._rail._sell_btn.click()
    ref = payloads[0]["order_ref"]

    w.order_status_updated.emit(_status_event(ref, "Filled", fill="3.12"))
    assert t._session.status_text(ref) == "Filled @ 3.12"
    assert not t._session.can_cancel(ref)

    # Second order from the same window gets its own row (newest on
    # top); a whatIf rejection surfaces with the reason as tooltip.
    t._submit_relock.stop()
    t._recompute()
    t._rail._sell_btn.click()
    ref2 = payloads[1]["order_ref"]
    assert ref2 != ref and len(t._session._rows) == 2
    assert t._session._model.row_data(0)["ref"] == ref2
    w.order_status_updated.emit(
        _status_event(ref2, "Rejected", err="Insufficient margin"))
    assert t._session.status_text(ref2) == "Rejected"
    assert t._session._rows[ref2]["error"] == "Insufficient margin"
    assert not t._session.can_cancel(ref2)


def test_chain_column_widths_persist_across_opens(qtbot, monkeypatch):
    from src.core import config_manager
    store = {}
    monkeypatch.setattr(config_manager, "load_layout",
                        lambda path=None: dict(store))
    monkeypatch.setattr(config_manager, "save_layout",
                        lambda layout, path=None: store.update(layout))

    t, w = _make(qtbot)
    view = t._chain_view
    # Simulate the first layout pass having run (offscreen widgets never
    # get a real resizeEvent), then a user drag on the BID column.
    view._widths_ready = True
    view.setColumnWidth(1, 153)
    assert view._save_debounce.isActive()      # drag → debounced save
    view._save_debounce.stop()
    view._save_widths()
    assert store["options_chain_columns"][1] == 153

    # A fresh ticket restores the saved widths immediately.
    t2, w2 = _make(qtbot)
    assert t2._chain_view._widths_ready
    assert t2._chain_view.columnWidth(1) == 153


def test_body_splitter_sizes_persist(qtbot, monkeypatch):
    from src.core import config_manager
    store = {}
    monkeypatch.setattr(config_manager, "load_layout",
                        lambda path=None: dict(store))
    monkeypatch.setattr(config_manager, "save_layout",
                        lambda layout, path=None: store.update(layout))

    t, w = _make(qtbot)
    t.resize(1200, 700)
    t.show()
    qtbot.waitExposed(t)
    t._body_split.setSizes([700, 398])
    t._save_splitter()
    saved = store["options_ticket_splitter"]
    assert saved == [int(s) for s in t._body_split.sizes()]
    assert len(saved) == 2 and all(s > 0 for s in saved)
    t.close()

    # A fresh ticket applies the saved divider position on construction
    # (pending sizes land on the first real layout pass, i.e. show).
    t2, w2 = _make(qtbot)
    t2.resize(1200, 700)
    t2.show()
    qtbot.waitExposed(t2)
    assert t2._body_split.sizes() == saved


def test_window_size_persisted_across_opens(qtbot, monkeypatch):
    from src.core import config_manager
    store = {}
    monkeypatch.setattr(config_manager, "load_layout",
                        lambda path=None: dict(store))
    monkeypatch.setattr(config_manager, "save_layout",
                        lambda layout, path=None: store.update(layout))
    t, w = _make(qtbot)
    t.resize(1200, 800)
    t.reject()
    assert store["options_ticket"] == {"w": 1200, "h": 800}
    # A new ticket restores the remembered size.
    t2, _ = _make(qtbot)
    assert (t2.width(), t2.height()) == (1200, 800)


def test_close_cancels_quotes(qtbot):
    t, w = _make(qtbot)
    _deliver_chain(t, w)
    _deliver_contracts(t, w)
    before = w.quote_cancels
    t.reject()
    assert w.quote_cancels > before


# ── Symbol chips: position group + searched group ────────────────────────────

def test_position_chips_top5_over_100_shares(qtbot, monkeypatch):
    monkeypatch.setattr(SymbolChipBar, "_load_chips",
                        staticmethod(lambda: []))
    positions = [
        {"symbol": "MU", "position": 300.0, "market_value": 249_000.0},
        {"symbol": "NVDA", "position": 200.0, "market_value": 39_000.0},
        {"symbol": "TSLA", "position": 300.0, "market_value": 91_000.0},
        {"symbol": "SPCX", "position": 500.0, "market_value": 58_000.0},
        {"symbol": "MSFT", "position": 55.0, "market_value": 99_000.0},
        {"symbol": "BIL", "position": 100.0, "market_value": 9_000.0},
        {"symbol": "GOOG", "position": 150.0, "market_value": 25_000.0},
    ]
    bar = SymbolChipBar(lambda s: 0.0, get_positions=lambda: positions)
    qtbot.addWidget(bar)
    # Top-5 by market value among holdings ≥100 shares; MSFT (55 sh)
    # excluded even though its market value ranks high.
    assert bar._order == ["MU", "TSLA", "SPCX", "NVDA", "GOOG"]
    assert bar.active_symbol() == ""      # nothing selected until the user picks


def test_searched_chips_append_right_persist_and_reorder(qtbot, monkeypatch):
    monkeypatch.setattr(SymbolChipBar, "_load_chips",
                        staticmethod(lambda: []))
    saved = []
    monkeypatch.setattr(
        SymbolChipBar, "_save_chips",
        lambda self: saved.append(list(self._custom)))
    positions = [
        {"symbol": "MU", "position": 300.0, "market_value": 100.0},
        {"symbol": "TSLA", "position": 200.0, "market_value": 50.0},
    ]
    bar = SymbolChipBar(lambda s: 0.0, get_positions=lambda: positions)
    qtbot.addWidget(bar)
    assert bar._order == ["MU", "TSLA"]
    # A searched symbol lands to the right of the position chips and
    # is persisted; a symbol already shown as a position chip is not
    # duplicated into the searched group.
    bar.select("QQQ")
    assert bar._order == ["MU", "TSLA", "QQQ"]
    assert saved[-1] == ["QQQ"]
    bar.select("TSLA")
    assert bar._order == ["MU", "TSLA", "QQQ"]
    assert saved[-1] == ["QQQ"]
    # Ladder-style drag reorder: drop QQQ onto MU → takes MU's slot.
    bar._on_chip_drop("QQQ", "MU")
    assert bar._order == ["QQQ", "MU", "TSLA"]
    # Right-click removes a searched chip outright; position chips stay.
    bar._remove_chip("MU")
    assert bar._order == ["QQQ", "MU", "TSLA"]
    bar._remove_chip("QQQ")
    assert bar._order == ["MU", "TSLA"]
    assert saved[-1] == []


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
