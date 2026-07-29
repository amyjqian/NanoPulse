"""
Options Income ticket — the QDialog (spec: NanoPulse-Options-Income.md).

Orchestrates the selector widgets, the strike ladder and the order rail:
fetches the chain (``reqSecDefOptParams``), resolves the listed strikes
for the chosen expiry/right (``reqContractDetails``), streams bid/ask +
delta for the visible strikes (OTM + ITM) and the underlying, recomputes every
derived figure (Decimal) on any change, and emits an
``order_requested``-shaped payload on ``order_ready``:

    {action: "SELL", type: "LMT", sec_type: "OPT",
     "_resolved_contract": {...}, "_target_accounts": [account],
     "_what_if_check": True, ...}

``_what_if_check`` asks FlashTradeView to route through
``worker.place_option_order`` (whatIf margin pre-check before transmit).
All worker responses are tokened; stale ones are dropped.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from src.ui.theme import Theme
from src.core import config_manager
from src.core import options_income as oi

from .chain_table import ChainView
from .parts import ExpirySelector, RightToggle, SymbolChipBar
from .rail import OrderRail

log = logging.getLogger(__name__)

_RIGHT = oi.STRATEGY_RIGHT       # strategy → "C" | "P"
_ACTION = oi.STRATEGY_ACTION     # strategy → "BUY" | "SELL"
_RIGHT_WORD = {"C": "CALL", "P": "PUT"}


class OptionsIncomeTicket(QDialog):
    """Covered-call / cash-secured-put order ticket."""

    order_ready = Signal(dict)

    def __init__(
        self,
        *,
        account: str,
        get_worker,               # Callable[[str], IBWorkerThread | None]
        get_buying_power,         # Callable[[str], float]
        get_position,             # Callable[[str, str], dict | None]
        symbol: str | None = None,
        is_margin: bool = True,
        display_account: str | None = None,   # alias shown in the header
        get_account_positions=None,   # Callable[[], list[dict]] — chips
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._account = account
        self._display_account = display_account or account
        self._get_account_positions = get_account_positions
        self._get_worker = get_worker
        self._get_buying_power = get_buying_power
        self._get_position = get_position
        self._is_margin = bool(is_margin)

        self._worker = None
        self._wired = False
        self._token = 0

        self._symbol = ""
        self._underlying_conid = 0
        self._spot = 0.0
        self._prev_close = 0.0
        self._shares = 0.0
        self._basis = 0.0
        self._chain: dict | None = None
        self._selected_leg: dict | None = None
        self._price_pending = True   # reset price to mid on next leg quote

        self.setWindowTitle("Options income")
        self.setModal(True)
        self.setMinimumSize(920, 620)
        # Restore the last user-chosen window size (saved on close).
        try:
            geo = config_manager.load_layout().get("options_ticket") or {}
            w, h = int(geo.get("w") or 0), int(geo.get("h") or 0)
            if w >= 920 and h >= 620:
                self.resize(w, h)
        except Exception:
            log.debug("Options ticket: could not restore size", exc_info=True)
        self.setStyleSheet(
            f"QDialog{{ background:{Theme.BG_PANEL}; color:{Theme.TEXT}; }}")

        self._build_ui()
        self._wire_worker()
        self._chips.symbol_selected.connect(self._load_symbol)
        # Only a symbol-specific launch (position row context menu)
        # pre-selects a chip; at account level the bar starts unselected.
        start = (symbol or "").upper()
        if start:
            if start != self._chips.active_symbol():
                self._chips.select(start)   # emits → _load_symbol
            else:
                self._load_symbol(start)
        else:
            # No symbol yet — still show the next four weekly expiries
            # (calendar Fridays) so the row isn't empty; the real chain
            # replaces them once a chip is picked.
            chips, more = oi.organize_expirations(
                oi.upcoming_fridays(date.today()), date.today())
            self._expiry.set_expiries(chips, more)
            self._set_status("Select a symbol to load its option chain.")

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        # ── Header ──────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)
        self._h_symbol = QLabel("—", self)
        self._h_symbol.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:17px; font-weight:bold;")
        self._h_price = QLabel("", self)
        self._h_price.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:15px;")
        self._h_change = QLabel("", self)
        self._h_pos = QLabel("", self)
        self._h_pos.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:11px;")
        self._h_account = QLabel(self._display_account, self)
        self._h_account.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:11px;"
            " font-family:Menlo, Consolas, monospace;")
        self._h_margin = QLabel("MARGIN", self)
        self._h_margin.setStyleSheet(
            "color:#58A6FF; background:rgba(88,166,255,0.12); border:none;"
            " border-radius:4px; padding:2px 8px; font-size:9px;"
            " font-weight:bold; letter-spacing:1px;")
        self._h_margin.setVisible(self._is_margin)
        close_btn = QPushButton("✕", self)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton{ background:transparent; border:none;"
            f" color:{Theme.TEXT_DIM}; font-size:14px; }}"
            f"QPushButton:hover{{ color:{Theme.TEXT}; }}")
        close_btn.clicked.connect(self.reject)
        header.addWidget(self._h_symbol)
        header.addWidget(self._h_price)
        header.addWidget(self._h_change)
        header.addSpacing(6)
        header.addWidget(self._h_pos)
        header.addSpacing(6)
        header.addWidget(self._h_account)
        header.addWidget(self._h_margin)
        header.addStretch(1)
        header.addWidget(close_btn)
        outer.addLayout(header)

        # ── Symbol chips + search ───────────────────────────────────────
        self._chips = SymbolChipBar(
            self._shares_for_symbol,
            get_positions=self._get_account_positions, parent=self)
        outer.addWidget(self._chips)

        # ── Calls/Puts toggle + expiry chips ────────────────────────────
        erow = QHBoxLayout()
        erow.setSpacing(12)
        self._right_toggle = RightToggle(self)
        self._right_toggle.right_changed.connect(self._on_strategy_changed)
        erow.addWidget(self._right_toggle)
        self._expiry = ExpirySelector(self)
        self._expiry.expiry_changed.connect(self._on_expiry_changed)
        erow.addWidget(self._expiry, stretch=1)
        outer.addLayout(erow)

        # ── Chain table + order rail ────────────────────────────────────
        body = QHBoxLayout()
        body.setSpacing(12)
        self._chain_view = ChainView(self)
        self._chain_view.row_selected.connect(self._on_row_selected)
        body.addWidget(self._chain_view, stretch=1)
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
        self._rail.side_changed.connect(self._on_strategy_changed)
        self._rail.cancel_clicked.connect(self.reject)
        self._rail.submit_clicked.connect(self._on_submit)
        body.addWidget(self._rail)
        outer.addLayout(body, stretch=1)

        # Status line (chain errors, offline account, …)
        self._status = QLabel("", self)
        self._status.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:10px;")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._chips.search_requested.connect(self._on_search_requested)

    # ── Worker wiring ───────────────────────────────────────────────────────

    def _wire_worker(self) -> None:
        self._worker = self._get_worker(self._account)
        if self._worker is None:
            self._set_status(
                "Account offline — connect it on the Conn page first.",
                error=True)
            return
        w = self._worker
        w.option_chain_results.connect(self._on_chain_results)
        w.option_chain_failed.connect(self._on_request_failed)
        w.option_contracts_results.connect(self._on_contracts_results)
        w.option_contracts_failed.connect(self._on_request_failed)
        w.option_quote_updated.connect(self._on_quote)
        w.symbol_search_results.connect(self._chips.set_search_results)
        self._wired = True

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
            (w.symbol_search_results, self._chips.set_search_results),
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

    def _save_geometry(self) -> None:
        """Remember the user-chosen window size across opens."""
        try:
            layout = config_manager.load_layout()
            layout["options_ticket"] = {"w": self.width(),
                                        "h": self.height()}
            config_manager.save_layout(layout)
        except Exception:
            log.debug("Options ticket: could not save size", exc_info=True)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_geometry()
        self._unwire()
        super().closeEvent(event)

    def reject(self) -> None:
        self._save_geometry()
        self._unwire()
        super().reject()

    def accept(self) -> None:
        self._save_geometry()
        self._unwire()
        super().accept()

    def _on_search_requested(self, token: int, text: str) -> None:
        if self._worker is not None:
            self._worker.request_symbol_search(token, text)

    # ── Symbol / position ───────────────────────────────────────────────────

    def _shares_for_symbol(self, symbol: str) -> float:
        pos = self._get_position(self._account, symbol)
        return float(pos.get("position") or 0.0) if pos else 0.0

    def _load_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        self._symbol = symbol
        pos = self._get_position(self._account, symbol) or {}
        self._shares = float(pos.get("position") or 0.0)
        self._basis = float(pos.get("avg_cost") or 0.0)
        self._underlying_conid = int(pos.get("con_id") or 0)
        self._spot = float(pos.get("market_price") or 0.0)
        self._prev_close = 0.0
        self._chain = None
        self._selected_leg = None
        self._price_pending = True
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

    def _refresh_header(self) -> None:
        self._h_symbol.setText(self._symbol or "—")
        self._h_price.setText(f"{self._spot:,.2f}" if self._spot > 0 else "")
        if self._spot > 0 and self._prev_close > 0:
            chg = self._spot - self._prev_close
            pct = chg / self._prev_close * 100
            color = Theme.ACCENT if chg >= 0 else "#F85149"
            self._h_change.setText(f"{chg:+,.2f} ({pct:+.2f}%)")
            self._h_change.setStyleSheet(f"color:{color}; font-size:12px;")
        else:
            self._h_change.setText("")
        # Share count already lives on the symbol chip — the header only
        # shows the cost basis, and nothing at all for flat symbols.
        self._h_pos.setText(
            f"Avg price @ {self._basis:,.2f}"
            if self._shares >= 1 and self._basis > 0 else "")

    def _strategy(self) -> str:
        """BC / CC / BP / CSP — derived from the rail's Buy/Sell toggle
        and the Calls/Puts toggle."""
        if self._rail.action() == "BUY":
            return "BC" if self._right_toggle.right() == "C" else "BP"
        return "CC" if self._right_toggle.right() == "C" else "CSP"

    def _current_status(self) -> oi.StrategyStatus:
        strategy = self._strategy()
        bp = float(self._get_buying_power(self._account) or 0.0)
        if strategy == "CC":
            return oi.cc_status(self._shares, is_margin=self._is_margin,
                                buying_power=bp)
        if strategy == "CSP":
            return oi.csp_status(bp, is_margin=self._is_margin)
        return oi.buy_status(bp)

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
                "credit": None, "yield_pct": None, "ann_pct": None,
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
        return f"{self._symbol} {self._spot:,.2f}" if self._spot > 0 else ""

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
            self._refresh_header()
            if self._spot > 0:
                self._chain_view.chain_model.set_sep_label(self._sep_label())
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
        strategy = self._strategy()
        ref = self._spot if strategy == "CC" else strike
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
            if _ACTION[strategy] == "SELL":
                y = oi.yield_pct(mid, ref)
                patch.update({
                    "yield_pct": float(y),
                    "ann_pct": float(oi.annualized_pct(y, dte)),
                })
            else:
                # Premium yield is an income (selling) metric — blank
                # for long strategies.
                patch.update({"yield_pct": None, "ann_pct": None})
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
        self._price_pending = True
        self._selected_leg = None
        self._request_contracts()
        self._recompute()

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
        strategy = self._strategy()
        bp = float(self._get_buying_power(self._account) or 0.0)
        if strategy == "CC":
            cap = oi.cc_capacity(self._shares)
            if self._is_margin:
                # Margin accounts may sell past share coverage (naked);
                # the whatIf pre-check is the real gate at submit.
                cap += oi.naked_capacity(bp, self._spot)
            return cap
        if strategy == "CSP":
            strike = (self._selected_leg or {}).get("strike") or 0.0
            return oi.csp_capacity(bp, strike)
        # Long call / put — capped by the premium cost per contract.
        px = float(self._rail.price() or 0)
        if px <= 0:
            px = float((self._selected_leg or {}).get("mid") or 0)
        return oi.buy_capacity(bp, px, self._leg_multiplier())

    def _recompute(self) -> None:
        strategy = self._strategy()
        action = _ACTION[strategy]
        status = self._current_status()
        leg = self._selected_leg
        exp = self._expiry.selected()
        max_qty = self._max_qty()
        self._rail.set_max_qty(max_qty)
        qty = self._rail.qty()
        px = self._rail.price()
        market = self._rail.is_market()
        if market and px <= 0 and leg:
            # Market order before any quote arrived — estimate at mid.
            px = oi.to_dec(leg.get("mid") or 0)
        mult = self._leg_multiplier()

        if leg and exp:
            strike = leg["strike"]
            word = _RIGHT_WORD[_RIGHT[strategy]]
            # Just the contract — side/qty/price already live on the
            # rail's own controls and the Sell button.
            self._rail.set_order_line(
                f"{self._symbol} {exp.chip_label} {strike:g} {word}")
        else:
            self._rail.set_order_line("—")

        if leg and px > 0:
            strike = oi.to_dec(leg["strike"])
            total = oi.credit(px, qty, mult)
            if strategy == "CC":
                be = oi.break_even_cc(self._basis, px)
                outcome = oi.if_called_away(
                    strike, self._basis, px, qty, mult)
                if qty * mult <= self._shares:
                    note = (f"Covered — {qty * mult} of your "
                            f"{int(self._shares):,} shares back this "
                            "position; they stay in the account unless "
                            "assigned.")
                    reserved = (f"Shares reserved {qty * mult} of "
                                f"{int(self._shares):,}")
                else:
                    covered_sh = int(self._shares) // mult * mult
                    note = (f"{covered_sh:,} shares cover part of this "
                            "position; the remainder is margin-backed "
                            "(naked) — IB's margin check runs before "
                            "transmit.")
                    reserved = f"Shares {covered_sh:,} + margin"
                self._rail.set_detail(
                    "Break-even", f"${be:,.2f}",
                    "If called away", f"{'+' if outcome >= 0 else ''}"
                                      f"${outcome:,.0f}", note)
            elif strategy == "CSP":
                be = oi.break_even_csp(strike, px)
                cost = oi.if_assigned_cost(strike, px, qty, mult)
                self._rail.set_detail(
                    "Break-even", f"${be:,.2f}",
                    "If assigned, cost", f"${cost:,.0f}",
                    f"Cash-secured — ${float(oi.collateral_csp(strike, qty, mult)):,.0f} "
                    "of buying power backs this position until expiry "
                    "or close.")
                reserved = (f"BP reserved "
                            f"${float(oi.collateral_csp(strike, qty, mult)):,.0f}")
            elif strategy == "BC":
                be = oi.break_even_long_call(strike, px)
                self._rail.set_detail(
                    "Break-even", f"${be:,.2f}",
                    "Max loss", f"−${total:,.0f}",
                    f"Long call — the right to buy {qty * mult} shares "
                    f"at {float(strike):g} until expiry; risk is limited "
                    "to the premium paid.")
                reserved = f"Cost ${total:,.0f} from buying power"
            else:                                      # BP — long put
                be = oi.break_even_long_put(strike, px)
                self._rail.set_detail(
                    "Break-even", f"${be:,.2f}",
                    "Max loss", f"−${total:,.0f}",
                    f"Long put — the right to sell {qty * mult} shares "
                    f"at {float(strike):g} until expiry; risk is limited "
                    "to the premium paid.")
                reserved = f"Cost ${total:,.0f} from buying power"
            if action == "SELL":
                ref = self._spot if strategy == "CC" else float(strike)
                y = oi.yield_pct(px, ref)
                ann = oi.annualized_pct(y, exp.dte if exp else 0)
                self._rail.set_footer(
                    f"+${total:,.2f}", reserved,
                    f"Ann. {ann:.1f}%" if ann else "")
            else:
                self._rail.set_footer(f"−${total:,.2f}", reserved, "")
        else:
            self._rail.set_detail(
                "Break-even", "—",
                {"CC": "If called away", "CSP": "If assigned, cost",
                 "BC": "Max loss", "BP": "Max loss"}[strategy],
                "—", "")
            self._rail.set_footer("—", "", "")

        # Submit button state (§4.7): ineligible reason wins, then leg/qty/px.
        if not status.eligible:
            self._rail.set_sell_enabled(False, status.reason)
        elif max_qty <= 0:
            if strategy == "CC":
                reason = ("Not enough shares or margin" if self._is_margin
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
        color = "#e06c75" if error else Theme.TEXT_DIM
        self._status.setStyleSheet(f"color:{color}; font-size:10px;")
        self._status.setText(text)

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
        contract = dict(leg.get("contract") or {})
        payload = {
            "action": _ACTION[self._strategy()],
            "type": "MKT" if market else "LMT",
            "symbol": contract.get("symbol", self._symbol),
            "qty": int(qty),
            "limit_price": 0.0 if market else float(px),
            "last_price": float(leg.get("mid") or px),
            "sec_type": "OPT",
            "profit_take": None,
            "stop_loss": None,
            "trailing": None,
            "_resolved_contract": {
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
            },
            "_target_accounts": [self._account],
            "_what_if_check": True,
        }
        log.info("OptionsIncomeTicket: %s → %s",
                 self._strategy(), payload)
        self.order_ready.emit(payload)
        self.accept()
