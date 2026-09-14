"""
Options Income ticket — the "Options" tab beside Positions / Trades
(spec: NanoPulse-Options-Income.md).

Orchestrates the selector widgets, the strike ladder and the order rail:
fetches the chain (``reqSecDefOptParams``), resolves the listed strikes
for the chosen expiry/right (``reqContractDetails``), streams bid/ask +
delta for the visible strikes (OTM + ITM) and the underlying, recomputes every
derived figure (Decimal) on any change, and emits one
``order_requested``-shaped payload PER TARGET ACCOUNT on ``order_ready``:

    {action: "SELL", type: "LMT", sec_type: "OPT",
     "_resolved_contract": {...}, "_target_accounts": [account],
     "_what_if_check": True, ...}

``_what_if_check`` asks FlashTradeView to route through
``worker.place_option_order`` (whatIf margin pre-check before transmit).
All worker responses are tokened; stale ones are dropped.

Accounts: the ticket carries no account of its own — it follows whatever
the Account List has checked (All / a user group / individual cards),
handed in as ``get_accounts()``.  A submit sends the SAME qty to every
checked+connected account (like Buy/Sell in the order panel); MAX is the
smallest per-account capacity so no account is over-sized, and the
worker's whatIf pre-check still gates each one.  Chain/quote streaming
rides the highest-priority CONNECTED TWS connection (``get_data_worker``
— the same rule as the order panel's price feed), independent of which
accounts are checked: market-data entitlements belong to a connection,
so the ticket asks the one the rest of the app already gets prices
from.  Orders still go to each account's own connection.

Submitted orders show up in the Orders panel above this tab (live
status + Cancel) like every other order — the ticket keeps no order
list of its own.  The tab is permanent: quotes are dropped while it is
hidden and resumed on show.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from src.ui.widgets.order_panel_parts import TickerStrip
from src.core import config_manager
from src.core import options_income as oi

from .chain_table import ChainView
from .parts import ExpirySelector, RightToggle, SymbolChipBar
from .rail import OrderRail

log = logging.getLogger(__name__)

# Layout key for the ladder | rail divider.
_SPLIT_KEY = "options_ticket_splitter"

_RIGHT = oi.STRATEGY_RIGHT       # strategy → "C" | "P"
_ACTION = oi.STRATEGY_ACTION     # strategy → "BUY" | "SELL"
_RIGHT_WORD = {"C": "CALL", "P": "PUT"}


class OptionsIncomeTicket(QWidget):
    """Covered-call / cash-secured-put order ticket (embedded tab)."""

    order_ready = Signal(dict)

    def __init__(
        self,
        *,
        get_accounts,             # Callable[[], list[str]] — checked accounts
        get_worker,               # Callable[[str], IBWorkerThread | None]
        get_data_worker,          # Callable[[], IBWorkerThread | None]
        fanout_blocked=None,      # Callable[[], str]; "" → may submit
        get_buying_power,         # Callable[[str], float]
        get_position,             # Callable[[str, str], dict | None]
        get_positions,            # Callable[[list[str]], list[dict]] — chips
        is_margin_account=None,   # Callable[[str], bool]; None → all margin
        account_label=None,       # Callable[[str], str]; None → account id
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._get_accounts = get_accounts
        # Host-side reasons a submit is refused right now (admin role,
        # the "orders on all accounts" guard) — surfaced as the Submit
        # button's label rather than a status-bar message, since the
        # ticket re-checks on every quote tick.
        self._fanout_blocked = fanout_blocked or (lambda: "")
        self._get_worker = get_worker
        self._get_data_worker = get_data_worker
        self._get_buying_power = get_buying_power
        self._get_position = get_position
        self._get_positions = get_positions
        # Account type is not exposed by the summary feed today, so the
        # host leaves this None and every account runs the margin math
        # (naked calls allowed, capped by buying power) — exactly what
        # the popup ticket did.  The whatIf pre-check is the real gate.
        self._is_margin_account = is_margin_account or (lambda _a: True)
        self._account_label = account_label or (lambda a: a)

        self._worker = None
        self._wired = False
        self._token = 0

        self._symbol = ""
        self._underlying_conid = 0
        self._spot = 0.0
        self._prev_close = 0.0
        self._chain: dict | None = None
        self._selected_leg: dict | None = None
        self._price_pending = True   # reset price to mid on next leg quote
        # Contract results of the current (expiry, right) + the spot the
        # ladder was built with — lets the ladder re-centre when the
        # underlying's first quote arrives after a spot-less build.
        self._contracts_cache: list = []
        self._rows_spot = 0.0

        self.setObjectName("optionsIncomeTicket")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#optionsIncomeTicket{{ background:{Theme.BG_PANEL};"
            f" color:{Theme.TEXT}; }}")

        self._build_ui()
        self._chips.symbol_selected.connect(self._load_symbol)
        # No symbol yet — still show the next four weekly expiries
        # (calendar Fridays) so the row isn't empty; the real chain
        # replaces them once a chip is picked.
        chips, more = oi.organize_expirations(
            oi.upcoming_fridays(date.today()), date.today())
        self._expiry.set_expiries(chips, more)
        self.refresh_workers()
        if self._worker is not None:
            self._set_status("Select a symbol to load its option chain.")
        self._recompute()      # gate Submit from the start (no accounts…)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Two columns split by a draggable divider: LEFT = Calls/Puts +
        expiry chips over the strike ladder; RIGHT = the Book Trader's
        ticker strip (symbol · last · bid · ask · change · "+") → symbol
        chip grid → order rail.  Everything about *what* to trade sits
        directly above the Buy/Sell controls, in the same shape as the
        Book Trader column."""
        outer = QVBoxLayout(self)
        # Tight bottom margin: the ladder should run to the tab's edge.
        outer.setContentsMargins(18, 14, 18, 6)
        outer.setSpacing(10)

        # ── Ticker strip (right column, top) ────────────────────────────
        # No account / margin badge here: the ticket follows the Account
        # List selection, which the top tab bar already names.  "+"
        # opens the shared symbol-search popup.  No share / cost line
        # either — per-account holdings drive MAX, and the Positions tab
        # is where they are read.
        self._ticker = TickerStrip(self, no_pct_change_symbols=())
        self._ticker.add_symbol_clicked.connect(self._show_symbol_popup)

        # ── Symbol chips (right column, under the strip) ────────────────
        self._chips = SymbolChipBar(
            get_positions=lambda: self._get_positions(self._accounts()),
            parent=self)

        # ── Calls/Puts toggle + expiry chips (left column, top) ─────────
        erow = QHBoxLayout()
        erow.setSpacing(12)
        self._right_toggle = RightToggle(self)
        self._right_toggle.right_changed.connect(self._on_strategy_changed)
        erow.addWidget(self._right_toggle)
        self._expiry = ExpirySelector(self)
        self._expiry.expiry_changed.connect(self._on_expiry_changed)
        erow.addWidget(self._expiry, stretch=1)

        # ── Chain table | order column — resizable, remembered split ────
        self._body_split = QSplitter(Qt.Orientation.Horizontal, self)
        self._body_split.setHandleWidth(2)
        self._body_split.setStyleSheet(
            f"QSplitter::handle {{ background: {Theme.BORDER}; }}")
        left_host = QWidget(self._body_split)
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addLayout(erow)
        self._chain_view = ChainView(left_host)
        self._chain_view.row_selected.connect(self._on_row_selected)
        left.addWidget(self._chain_view, stretch=1)
        self._body_split.addWidget(left_host)
        # Scrolling shifts which strikes stream quotes (debounced so a
        # fling doesn't churn reqMktData/cancelMktData on every pixel).
        self._scroll_debounce = QTimer(self)
        self._scroll_debounce.setSingleShot(True)
        self._scroll_debounce.setInterval(350)
        self._scroll_debounce.timeout.connect(self._refresh_quote_subs)
        self._chain_view.verticalScrollBar().valueChanged.connect(
            self._on_ladder_scrolled)
        self._rail = OrderRail(self)
        self._rail.qty_changed.connect(lambda _n: self._recompute())
        self._rail.price_changed.connect(lambda _p: self._recompute())
        self._rail.side_changed.connect(self._on_side_changed)
        self._rail.submit_clicked.connect(self._on_submit)
        # Post-submit lockout: the ticket stays put for the next order,
        # so the submit button is held disabled briefly to swallow
        # double-clicks (parented to the ticket so it dies with it).
        self._submit_relock = QTimer(self)
        self._submit_relock.setSingleShot(True)
        self._submit_relock.setInterval(1500)
        self._submit_relock.timeout.connect(self._recompute)
        # Right pane: ticker strip, chips, then the order rail.  Order
        # status lives in the Orders panel above the tabs, not here.
        right_host = QWidget(self._body_split)
        right = QVBoxLayout(right_host)
        right.setContentsMargins(10, 0, 0, 0)
        right.setSpacing(10)
        right.addWidget(self._ticker)
        right.addWidget(self._chips)
        right.addWidget(self._rail, stretch=1)
        self._body_split.addWidget(right_host)
        self._body_split.setStretchFactor(0, 1)   # extra width → ladder
        self._body_split.setStretchFactor(1, 0)
        self._body_split.setCollapsible(0, False)
        self._body_split.setCollapsible(1, False)
        outer.addWidget(self._body_split, stretch=1)
        # Restore the last divider position; save it (debounced — a
        # drag emits splitterMoved per pixel) whenever it moves.
        try:
            layout = config_manager.load_layout()
            sizes = [int(s) for s in (layout.get(_SPLIT_KEY) or [])]
            if len(sizes) == 2 and all(s > 0 for s in sizes):
                self._body_split.setSizes(sizes)
        except Exception:
            log.debug("Options ticket: could not restore splitter",
                      exc_info=True)
        self._split_save = QTimer(self)
        self._split_save.setSingleShot(True)
        self._split_save.setInterval(400)
        self._split_save.timeout.connect(self._save_splitter)
        self._body_split.splitterMoved.connect(
            lambda _pos, _idx: self._split_save.start())

        # Status line (chain errors, offline account, …)
        # Shown only while there is something to say — an empty label
        # would still reserve a text row under the ladder.
        self._status = QLabel("", self)
        self._status.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:10px;")
        self._status.setWordWrap(True)
        self._status.hide()
        outer.addWidget(self._status)

    # ── Accounts ────────────────────────────────────────────────────────────

    _NO_DATA_MSG = ("No TWS connection is online — connect one on the "
                    "Conn page first.")

    def _accounts(self) -> list[str]:
        """The checked accounts that currently have a live connection —
        the fan-out targets.  Never cached: the Account List is the
        source of truth and the host calls :meth:`on_accounts_changed`
        whenever it moves."""
        try:
            checked = list(self._get_accounts() or [])
        except Exception:
            log.debug("Options ticket: get_accounts raised", exc_info=True)
            return []
        return [a for a in checked if self._get_worker(a) is not None]

    def on_accounts_changed(self) -> None:
        """Host hook: the checked accounts / active group changed.
        Re-derives the position chips and re-gates the rail (MAX is
        per-account).  The data connection does not follow the
        selection — see :meth:`refresh_workers`."""
        self._chips.refresh_counts()
        if self._symbol:
            self._seed_from_positions(self._symbol)
        self._recompute()

    def on_positions_changed(self) -> None:
        """Host hook (debounced): live position data changed — chip
        ranking and the per-account capacities follow it."""
        self._chips.refresh_counts()
        if self._symbol:
            self._seed_from_positions(self._symbol)
        self._recompute()

    # ── Worker wiring ───────────────────────────────────────────────────────

    def refresh_workers(self) -> None:
        """(Re)bind the data connection — the host's ``get_data_worker``
        (highest-priority connected TWS connection, the order panel's
        rule) — used for chain / strikes / quotes / symbol search.
        Idempotent: the same instance is left alone; a different one (a
        higher-priority connection came online, or a reconnect replaced
        the thread) is swapped in and the loaded chain re-fetched.
        Called on construction, from the host's cold-start
        ``_wire_all_workers`` retry and on show."""
        worker = self._get_data_worker()
        if worker is self._worker:
            if worker is None:
                self._set_status(self._NO_DATA_MSG, error=True)
            return
        self._unwire()
        self._worker = worker
        if worker is None:
            self._set_status(self._NO_DATA_MSG, error=True)
            return
        w = worker
        w.option_chain_results.connect(self._on_chain_results)
        w.option_chain_failed.connect(self._on_request_failed)
        w.option_contracts_results.connect(self._on_contracts_results)
        w.option_contracts_failed.connect(self._on_request_failed)
        w.option_quote_updated.connect(self._on_quote)
        self._wired = True
        if self._symbol:
            # New connection → fresh chain + quotes for the loaded symbol.
            self._load_symbol(self._symbol)
        else:
            self._set_status("Select a symbol to load its option chain.")

    def _unwire(self) -> None:
        if not self._wired or self._worker is None:
            return
        w = self._worker
        for sig, slot in (
            (w.option_chain_results, self._on_chain_results),
            (w.option_chain_failed, self._on_request_failed),
            (w.option_contracts_results, self._on_contracts_results),
            (w.option_contracts_failed, self._on_request_failed),
            (w.option_quote_updated, self._on_quote),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            w.cancel_option_quotes()
        except RuntimeError:
            pass
        self._wired = False

    def _collect_splitter_sizes(self, layout: dict) -> None:
        sizes = [int(s) for s in self._body_split.sizes()]
        if len(sizes) == 2 and all(s > 0 for s in sizes):
            layout[_SPLIT_KEY] = sizes

    def _save_splitter(self) -> None:
        try:
            layout = config_manager.load_layout()
            self._collect_splitter_sizes(layout)
            config_manager.save_layout(layout)
        except Exception:
            log.debug("Options ticket: could not save splitter",
                      exc_info=True)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._unwire()
        super().closeEvent(event)

    # Option quote lines are a shared, capped IB resource (~100 per
    # account, alongside the rest of the app), so the ladder streams
    # only while the Options tab is the visible one.  Any exception in
    # a Qt virtual override trips a Shiboken bug on macOS that turns it
    # into a segfault — catch + log.
    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        try:
            self.refresh_workers()
            self._refresh_quote_subs()
        except Exception:
            log.exception("Options ticket: showEvent raised")

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        try:
            if self._worker is not None:
                self._worker.cancel_option_quotes()
        except Exception:
            log.exception("Options ticket: hideEvent raised")
        super().hideEvent(event)

    def _show_symbol_popup(self) -> None:
        """"+" on the ticker strip → the shared symbol-search popup
        (saved contracts instantly, live IBKR stock matches as the user
        types), stocks only — the ticket trades US stock options.  A
        pick becomes a searched chip and loads its chain."""
        from src.ui.widgets.symbol_search_popup import (
            SymbolSearchPopup,
            ensure_contract_saved,
        )
        # Same connection the chain streams on — it is the one with
        # market-data entitlements, and reqMatchingSymbols needs none.
        workers = [self._worker] if self._worker is not None else []
        dlg = SymbolSearchPopup(
            self, workers=workers,
            exclude={s.upper() for s in self._chips.symbols()},
            stocks_only=True)

        def _picked(entry: dict) -> None:
            sym, _meta = ensure_contract_saved(entry)
            if sym:
                self._chips.select(sym)

        dlg.symbol_selected.connect(_picked)
        # Anchor below the strip's "+" button.
        dlg.open_below(self._ticker._btn_add)   # noqa: SLF001 — same as BookTrader

    # ── Symbol / position ───────────────────────────────────────────────────

    def _position_hint(self, symbol: str) -> tuple[int, float]:
        """``(con_id, market_price)`` for *symbol* from the first
        checked+connected account that holds it — seeds the underlying
        conId and an initial spot before the chain / quote arrive.
        Share counts are NOT aggregated: MAX is sized per account in
        :meth:`_account_cap`."""
        con_id = 0
        mkt = 0.0
        for account in self._accounts():
            pos = self._get_position(account, symbol) or {}
            if not con_id:
                con_id = int(pos.get("con_id") or 0)
            if mkt <= 0:
                mkt = float(pos.get("market_price") or 0.0)
            if con_id and mkt > 0:
                break
        return con_id, mkt

    def _seed_from_positions(self, symbol: str) -> None:
        """Fill in a missing underlying conId / spot from a held position
        (accounts or positions moved under a loaded chain)."""
        con_id, mkt = self._position_hint(symbol)
        if not self._underlying_conid and con_id:
            self._underlying_conid = con_id
        if self._spot <= 0 and mkt > 0:
            self._spot = mkt
            self._refresh_header()

    def _load_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        self._symbol = symbol
        con_id, mkt = self._position_hint(symbol)
        self._underlying_conid = con_id
        self._spot = mkt
        self._prev_close = 0.0
        self._chain = None
        self._selected_leg = None
        self._price_pending = True
        # Stale contracts must never seed a spot-arrival rebuild for the
        # new symbol.
        self._contracts_cache = []
        self._rows_spot = 0.0
        # New symbol → expiry snaps back to the nearest weekly.
        self._expiry.reset_selection()
        self._chain_view.chain_model.set_rows([])
        self._refresh_header()
        if self._worker is None:
            return
        self._token += 1
        self._set_status(f"Loading option chain for {symbol}…")
        self._worker.cancel_option_quotes()
        self._worker.request_option_chain(self._token, {
            "symbol": symbol,
            "con_id": self._underlying_conid,
            "sec_type": "STK",
        })
        self._recompute()

    def _refresh_header(self, bid: float = 0.0, ask: float = 0.0) -> None:
        """Push symbol / spot / bid / ask / prior close into the ticker
        strip (it formats the change badges itself)."""
        self._ticker.set_symbol(self._symbol)
        self._ticker.update_price(self._spot, bid, ask, self._prev_close)

    def _strategy(self) -> str:
        """BC / CC / BP / CSP — derived from the rail's Buy/Sell toggle
        and the Calls/Puts toggle."""
        if self._rail.action() == "BUY":
            return "BC" if self._right_toggle.right() == "C" else "BP"
        return "CC" if self._right_toggle.right() == "C" else "CSP"

    def _account_cap(self, account: str,
                     strategy: str) -> tuple[int, oi.StrategyStatus]:
        """``(max contracts, eligibility)`` for ONE account under the
        current strategy, from that account's own shares, buying power
        and account type — the per-account math the popup ticket ran for
        its single account."""
        pos = (self._get_position(account, self._symbol) or {}
               if self._symbol else {})
        shares = float(pos.get("position") or 0.0)
        bp = float(self._get_buying_power(account) or 0.0)
        margin = bool(self._is_margin_account(account))
        if strategy == "CC":
            status = oi.cc_status(shares, is_margin=margin, buying_power=bp)
            cap = oi.cc_capacity(shares)
            if margin:
                # Margin accounts may sell past share coverage (naked);
                # the whatIf pre-check is the real gate at submit.
                cap += oi.naked_capacity(bp, self._spot)
            return cap, status
        if strategy == "CSP":
            strike = (self._selected_leg or {}).get("strike") or 0.0
            return (oi.csp_capacity(bp, strike),
                    oi.csp_status(bp, is_margin=margin))
        # Long call / put — capped by the premium cost per contract.
        px = float(self._rail.price() or 0)
        if px <= 0:
            px = float((self._selected_leg or {}).get("mid") or 0)
        return (oi.buy_capacity(bp, px, self._leg_multiplier()),
                oi.buy_status(bp))

    def _current_status(self) -> oi.StrategyStatus:
        """Eligible only when EVERY checked+connected account is — the
        same qty goes to each, so one account that can't take the order
        blocks the fan-out (its reason names it when several are
        checked)."""
        accounts = self._accounts()
        if not accounts:
            return oi.StrategyStatus(False, "NO ACCOUNTS", "",
                                     "No accounts selected")
        blocked = self._fanout_blocked()
        if blocked:
            return oi.StrategyStatus(False, "BLOCKED", "", blocked)
        strategy = self._strategy()
        first = None
        for account in accounts:
            _cap, status = self._account_cap(account, strategy)
            if not status.eligible:
                if len(accounts) == 1:
                    return status
                reason = status.reason or status.badge
                return oi.StrategyStatus(
                    False, status.badge, status.detail,
                    f"{self._account_label(account)} – {reason}")
            first = first or status
        return first

    # ── Chain / contracts / quotes ──────────────────────────────────────────

    def _on_chain_results(self, token: int, chain: dict) -> None:
        if token != self._token:
            return
        self._chain = chain
        self._underlying_conid = int(
            chain.get("underlying_conid") or self._underlying_conid)
        self._set_status("")
        chips, more = oi.organize_expirations(
            chain.get("expirations", []), date.today())
        self._expiry.set_expiries(chips, more)
        if self._expiry.selected() is None:
            self._set_status("No listed expirations for this symbol.",
                             error=True)
            self._recompute()
            return
        self._request_contracts()

    def _request_contracts(self) -> None:
        if self._worker is None or self._chain is None:
            return
        exp = self._expiry.selected()
        if exp is None:
            return
        self._token += 1
        self._set_status("Loading strikes…")
        self._worker.request_option_contracts(self._token, {
            "symbol": self._symbol,
            "expiry": exp.ymd,
            "right": self._right_toggle.right(),
            "currency": "USD",
            "trading_class": self._chain.get("trading_class", ""),
        })

    def _on_contracts_results(self, token: int, results: list) -> None:
        if token != self._token:
            return
        right = self._right_toggle.right()
        # Prefer the standard trading class (skips AAPL7-style adjusted
        # contracts with non-standard deliverables).
        std = [r for r in results
               if not r.get("trading_class")
               or r.get("trading_class") == self._symbol]
        pool = std or results
        by_strike = {}
        for r in pool:
            s = float(r.get("strike") or 0.0)
            if s > 0 and r.get("right") == right:
                by_strike.setdefault(s, r)
        if not by_strike:
            self._chain_view.chain_model.set_rows([])
            self._set_status(
                "No listed contracts for this expiry — pick another.",
                error=True)
            self._recompute()
            return

        spot = self._spot or 0.0
        self._contracts_cache = list(results)
        self._rows_spot = spot
        strikes = oi.pick_strikes(list(by_strike), spot, right)
        rows = []
        for s in strikes:                      # descending — highest first
            r = by_strike[s]
            rows.append({
                "strike": s,
                "conid": int(r.get("conid") or 0),
                "contract": r,
                "delta": 0.0, "bid": None, "ask": None, "last": None,
                "mid": 0.0,
                "credit": None, "yield_pct": None,
            })
        # ATM separator between the ≥spot block and the <spot block
        # (ITM/OTM boundary — reversed sides for calls vs puts).
        if spot > 0:
            i_below = next((i for i, r in enumerate(rows)
                            if r["strike"] < spot), None)
            if i_below:                      # both sides present
                rows.insert(i_below, {"sep": True,
                                      "label": self._sep_label()})
        self._chain_view.chain_model.set_rows(rows)
        self._set_status("")
        self._price_pending = True
        self._chain_view.select_row(self._default_row(rows, spot, right))
        self._refresh_quote_subs()

    # The ladder holds up to ~100 strikes but IB caps concurrent market
    # data lines (~100 per account, shared with the rest of the app) —
    # so only the rows around the viewport and the selection stream, and
    # the batch is resubscribed as the user scrolls (debounced).
    _QUOTE_PAD = 10

    def _quote_rows(self) -> list[int]:
        view = self._chain_view
        model = view.chain_model
        count = model.rowCount()
        if count == 0:
            return []
        sel = view.currentIndex().row()
        if sel < 0:
            sel = count // 2
        lo, hi = sel - self._QUOTE_PAD, sel + self._QUOTE_PAD
        if view.isVisible():
            first = view.rowAt(0)
            last = view.rowAt(max(0, view.viewport().height() - 1))
            if first >= 0:
                if last < first:
                    last = count - 1
                lo = min(lo, first - self._QUOTE_PAD)
                hi = max(hi, last + self._QUOTE_PAD)
        return list(range(max(0, lo), min(count - 1, hi) + 1))

    def _refresh_quote_subs(self) -> None:
        """(Re)subscribe quotes: rows near the viewport/selection + the
        underlying.  Cheap to call repeatedly — the worker swaps the
        whole batch atomically and re-emits cached ticker data."""
        if self._worker is None:
            return
        model = self._chain_view.chain_model
        subs = []
        for i in self._quote_rows():
            rd = model.row_data(i)
            if rd and rd.get("contract"):
                subs.append(dict(rd["contract"]))
        if not subs:
            return
        if self._underlying_conid > 0:
            subs.append({"conid": self._underlying_conid,
                         "sec_type": "STK", "exchange": "SMART",
                         "currency": "USD"})
        self._worker.request_option_quotes(self._token, subs)

    def _on_ladder_scrolled(self) -> None:
        self._scroll_debounce.start()

    def _sep_label(self) -> str:
        return f"{self._spot:,.2f}" if self._spot > 0 else ""

    @staticmethod
    def _default_row(rows: list[dict], spot: float, right: str) -> int:
        """Default leg ≈ two strikes OTM of the money (the ~0.30-delta
        zone), on the descending-sorted ladder (separator row included
        in the indexing but never counted as a strike)."""
        if not rows:
            return 0
        if spot <= 0:
            return len(rows) // 2
        if right == "C":
            # Nearest OTM call = lowest strike ≥ spot (highest index of
            # the ≥spot block); more OTM = up the list (lower index).
            at = [i for i, r in enumerate(rows)
                  if not r.get("sep") and r["strike"] >= spot]
            anchor = max(at) if at else 0
            return max(0, anchor - 2)
        # Nearest OTM put = highest strike ≤ spot (lowest such index);
        # more OTM = down the list (higher index).
        at = [i for i, r in enumerate(rows)
              if not r.get("sep") and r["strike"] <= spot]
        anchor = min(at) if at else len(rows) - 1
        return min(len(rows) - 1, anchor + 2)

    def _on_quote(self, token: int, conid: int, q: dict) -> None:
        if token != self._token:
            return
        if conid == self._underlying_conid:
            last = q.get("last") or 0.0
            close = q.get("close") or 0.0
            if close > 0:
                self._prev_close = close
            if last > 0:
                self._spot = last
            elif q.get("bid") and q.get("ask"):
                self._spot = (q["bid"] + q["ask"]) / 2
            self._refresh_header(bid=q.get("bid") or 0.0,
                                 ask=q.get("ask") or 0.0)
            if self._spot > 0:
                self._chain_view.chain_model.set_sep_label(self._sep_label())
                # A ladder built before the spot arrived fell back to
                # the chain's LOWEST strikes (deep ITM, delta ≈ 1) —
                # rebuild it centred on the money now that we know it.
                if self._rows_spot <= 0 and self._contracts_cache:
                    self._on_contracts_results(
                        self._token, self._contracts_cache)
            return

        model = self._chain_view.chain_model
        row = model.row_for_conid(conid)
        if row < 0:
            return
        # None = IB sent no data for that field (≠ a real 0.00 quote) —
        # never blank a value the row already knows.
        bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
        mid = ((bid + ask) / 2 if (bid or 0) > 0 and (ask or 0) > 0
               else (last or 0.0))
        rd = model.row_data(row) or {}
        strike = rd.get("strike", 0.0)
        exp = self._expiry.selected()
        dte = exp.dte if exp else 0
        mult = self._leg_multiplier(rd)
        patch = {k: v for k, v in
                 (("bid", bid), ("ask", ask), ("last", last))
                 if v is not None}
        if mid > 0:
            patch["mid"] = mid
        # Delta: prefer IB's greek computation whenever one has arrived
        # (some ticks carry an empty delta — never blank a known value).
        # On delayed/paper data IB sends greeks for few strikes, so fall
        # back to a Black-Scholes estimate from the mid premium.
        if q.get("delta"):
            patch["delta"] = q["delta"]
            patch["delta_ib"] = True
        elif (not rd.get("delta_ib") and mid > 0
                and self._spot > 0 and dte > 0):
            est = oi.bs_delta(self._spot, strike, dte, mid,
                              self._right_toggle.right())
            if est:
                patch["delta"] = est
        if mid > 0:
            patch["credit"] = float(oi.credit(mid, 1, mult))
            patch.update(self._income_figures(mid, strike, dte))
        model.update_row(row, patch)

        if (self._selected_leg is not None
                and self._selected_leg.get("conid") == conid):
            # Row values post-merge — survives ticks missing a field.
            self._rail.set_bid_mid_ask(rd.get("bid") or 0,
                                       rd.get("mid") or 0,
                                       rd.get("ask") or 0)
            if self._price_pending and mid > 0:
                # Leg changed → price resets to the new leg's mid (§4.6).
                self._price_pending = False
                self._rail.set_price(mid, anchor="MID")
            self._recompute()

    def _on_request_failed(self, token: int, msg: str) -> None:
        if token != self._token:
            return
        self._set_status(msg, error=True)
        self._recompute()

    # ── Selection changes ───────────────────────────────────────────────────

    def _on_strategy_changed(self, _key: str) -> None:
        """Call ↔ Put — different contracts, so the strike list must be
        re-fetched from TWS."""
        self._price_pending = True
        self._selected_leg = None
        self._request_contracts()
        self._recompute()

    def _on_side_changed(self, _side: str) -> None:
        """Buy ↔ Sell — same (expiry, right) strike list, so no TWS
        round-trip / "Loading strikes…": keep the selection and price,
        just re-derive the ladder figures and the rail state locally."""
        self._refresh_row_figures()
        self._recompute()

    def _income_figures(self, mid: float, strike: float, dte: int) -> dict:
        """Per-row YIELD display field for the current strategy —
        premium yield is an income (selling) metric, blank on longs."""
        strategy = self._strategy()
        if _ACTION[strategy] != "SELL":
            return {"yield_pct": None}
        ref = self._spot if strategy == "CC" else strike
        y = oi.yield_pct(mid, ref)
        return {"yield_pct": float(y)}

    def _refresh_row_figures(self) -> None:
        """Re-derive yield/ann from cached row mids (side toggled)."""
        model = self._chain_view.chain_model
        exp = self._expiry.selected()
        dte = exp.dte if exp else 0
        for i in range(model.rowCount()):
            rd = model.row_data(i) or {}
            mid = rd.get("mid") or 0
            if rd.get("sep") or mid <= 0:
                continue
            model.update_row(i, self._income_figures(
                mid, float(rd.get("strike") or 0), dte))

    def _on_expiry_changed(self, _ymd: str) -> None:
        self._price_pending = True
        self._selected_leg = None
        self._request_contracts()

    def _on_row_selected(self, row: int) -> None:
        rd = self._chain_view.chain_model.row_data(row)
        if not rd or rd.get("sep"):
            return
        self._selected_leg = rd
        self._price_pending = True
        bid = rd.get("bid") or 0
        mid = rd.get("mid") or 0
        ask = rd.get("ask") or 0
        contract = rd.get("contract") or {}
        tick = float(contract.get("min_tick") or 0.0)
        if tick > 0:
            self._rail.set_tick(tick)
        self._rail.set_bid_mid_ask(bid, mid, ask)
        if mid > 0:
            self._price_pending = False
            self._rail.set_price(mid, anchor="MID")
        self._recompute()

    # ── Derived state ───────────────────────────────────────────────────────

    def _leg_multiplier(self, rd: dict | None = None) -> int:
        src = (rd or self._selected_leg or {}).get("contract") or {}
        for raw in (src.get("multiplier"),
                    (self._chain or {}).get("multiplier")):
            try:
                m = int(float(raw))
                if m > 0:
                    return m
            except (TypeError, ValueError):
                continue
        return oi.MULT

    def _max_qty(self) -> int:
        """The smallest per-account capacity — the same qty goes to every
        checked account, so none may be over-sized.  0 with no accounts."""
        accounts = self._accounts()
        if not accounts:
            return 0
        strategy = self._strategy()
        return min(self._account_cap(a, strategy)[0] for a in accounts)

    def _recompute(self) -> None:
        strategy = self._strategy()
        status = self._current_status()
        leg = self._selected_leg
        exp = self._expiry.selected()
        max_qty = self._max_qty()
        self._rail.set_max_qty(max_qty)
        px = self._rail.price()
        market = self._rail.is_market()
        if market and px <= 0 and leg:
            # Market order before any quote arrived — estimate at mid.
            px = oi.to_dec(leg.get("mid") or 0)

        if leg and exp:
            strike = leg["strike"]
            word = _RIGHT_WORD[_RIGHT[strategy]]
            # Just the contract — side/qty/price already live on the
            # rail's own controls and the Sell button.
            self._rail.set_order_line(
                f"{self._symbol} {exp.chip_label} {strike:g} {word}")
        else:
            self._rail.set_order_line("—")

        # Submit button state (§4.7): ineligible reason wins, then leg/qty/px.
        if not status.eligible:
            self._rail.set_sell_enabled(False, status.reason)
        elif max_qty <= 0:
            if strategy == "CC":
                margin = any(self._is_margin_account(a)
                             for a in self._accounts())
                if margin and self._spot <= 0:
                    # Naked capacity is BP ÷ (20 % × spot × 100) — with
                    # no underlying price yet it reads as 0, which is
                    # "no quote", not "no margin".
                    reason = f"Waiting for {self._symbol} price"
                else:
                    reason = ("Not enough shares or margin" if margin
                              else "Not enough shares")
            elif strategy in ("BC", "BP") and px <= 0:
                reason = ""     # cost unknown until a quote arrives
            else:
                reason = "Not enough buying power"
            self._rail.set_sell_enabled(False, reason)
        elif leg is None or (px <= 0 and not market):
            self._rail.set_sell_enabled(False)
        else:
            self._rail.set_sell_enabled(True)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        color = "#F85149" if error else Theme.TEXT_DIM
        self._status.setStyleSheet(f"color:{color}; font-size:10px;")
        self._status.setText(text)
        self._status.setVisible(bool(text))

    # ── Submit ──────────────────────────────────────────────────────────────

    def _on_submit(self) -> None:
        leg = self._selected_leg
        exp = self._expiry.selected()
        if leg is None or exp is None:
            return
        qty = self._rail.qty()
        px = self._rail.price()
        market = self._rail.is_market()
        max_qty = self._max_qty()
        # Structural clamp again right before transmit (§7).
        if not (0 < qty <= max_qty) or (px <= 0 and not market):
            return
        # Reject stale chain data: expiry must still be in the future.
        try:
            if datetime.strptime(exp.ymd, "%Y%m%d").date() < date.today():
                self._set_status("Expiry is in the past — reload the chain.",
                                 error=True)
                return
        except ValueError:
            return
        accounts = self._accounts()
        blocked = self._fanout_blocked() if accounts else "No accounts selected"
        if blocked:
            self._set_status(f"{blocked} — nothing submitted.", error=True)
            self._recompute()
            return
        contract = dict(leg.get("contract") or {})
        strategy = self._strategy()
        resolved = {
            "conid": contract.get("conid"),
            "symbol": contract.get("symbol", self._symbol),
            "sec_type": "OPT",
            "exchange": contract.get("exchange") or "SMART",
            "currency": contract.get("currency") or "USD",
            "local_symbol": contract.get("local_symbol", ""),
            "expiry": contract.get("expiry", exp.ymd),
            "multiplier": contract.get("multiplier", ""),
            "strike": leg.get("strike"),
            "right": contract.get("right"),
        }
        # One payload per checked account; the worker generates each
        # order's ref and the Orders panel tracks it from there.
        for account in accounts:
            payload = {
                "action": _ACTION[strategy],
                "type": "MKT" if market else "LMT",
                "symbol": contract.get("symbol", self._symbol),
                "qty": int(qty),
                "limit_price": 0.0 if market else float(px),
                "last_price": float(leg.get("mid") or px),
                "sec_type": "OPT",
                "profit_take": None,
                "stop_loss": None,
                "trailing": None,
                "_resolved_contract": dict(resolved),
                "_target_accounts": [account],
                "_what_if_check": True,
            }
            log.info("OptionsIncomeTicket: %s → %s", strategy, payload)
            self.order_ready.emit(payload)
        # The ticket stays put for the next order: qty snaps back to 1
        # so the next order is sized deliberately, and the submit button
        # is held down briefly so a double-click can't transmit twice.
        self._rail.reset_qty()
        self._rail.set_sell_enabled(False, "Order submitted ✓")
        self._submit_relock.start()
