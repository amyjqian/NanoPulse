"""
Options Income ticket — the QDialog (spec: NanoPulse-Options-Income.md).

Orchestrates the selector widgets, the strike ladder and the order rail:
fetches the chain (``reqSecDefOptParams``), resolves the listed strikes
for the chosen expiry/right (``reqContractDetails``), streams bid/ask +
delta for the six visible strikes and the underlying, recomputes every
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

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from src.ui.theme import Theme
from src.core import options_income as oi

from .chain_table import ChainView
from .parts import ExpirySelector, StrategyCards, SymbolChipBar
from .rail import OrderRail

log = logging.getLogger(__name__)

_RIGHT = {"CC": "C", "CSP": "P"}
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._account = account
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
        self.setStyleSheet(
            f"QDialog{{ background:{Theme.BG_PANEL}; color:{Theme.TEXT}; }}")

        self._build_ui()
        self._wire_worker()
        self._chips.symbol_selected.connect(self._load_symbol)
        start = (symbol or self._chips.active_symbol() or "").upper()
        if start:
            if start != self._chips.active_symbol():
                self._chips.select(start)   # emits → _load_symbol
            else:
                self._load_symbol(start)

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
        self._h_account = QLabel(self._account, self)
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
        self._chips = SymbolChipBar(self._shares_for_symbol, self)
        outer.addWidget(self._chips)

        # ── Strategy cards ──────────────────────────────────────────────
        self._cards = StrategyCards(self)
        self._cards.strategy_changed.connect(self._on_strategy_changed)
        outer.addWidget(self._cards)

        # ── Expiry chips ────────────────────────────────────────────────
        self._expiry = ExpirySelector(self)
        self._expiry.expiry_changed.connect(self._on_expiry_changed)
        outer.addWidget(self._expiry)

        # ── Chain table + order rail ────────────────────────────────────
        body = QHBoxLayout()
        body.setSpacing(12)
        self._chain_view = ChainView(self)
        self._chain_view.row_selected.connect(self._on_row_selected)
        body.addWidget(self._chain_view, stretch=1)
        self._rail = OrderRail(self)
        self._rail.qty_changed.connect(lambda _n: self._recompute())
        self._rail.price_changed.connect(lambda _p: self._recompute())
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

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._unwire()
        super().closeEvent(event)

    def reject(self) -> None:
        self._unwire()
        super().reject()

    def accept(self) -> None:
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
        self._chain_view.chain_model.set_rows([])
        self._refresh_header()
        self._refresh_cards()
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
        if self._shares >= 1:
            basis = f" @ {self._basis:,.2f}" if self._basis > 0 else ""
            self._h_pos.setText(f"{int(self._shares):,} shares{basis}")
        else:
            self._h_pos.setText("no shares")

    def _refresh_cards(self) -> None:
        bp = float(self._get_buying_power(self._account) or 0.0)
        self._cards.set_status(
            oi.cc_status(self._shares),
            oi.csp_status(bp, is_margin=self._is_margin))

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
            self._refresh_cards()
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
            "right": _RIGHT[self._cards.selected()],
            "currency": "USD",
            "trading_class": self._chain.get("trading_class", ""),
        })

    def _on_contracts_results(self, token: int, results: list) -> None:
        if token != self._token:
            return
        right = _RIGHT[self._cards.selected()]
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
        strikes = oi.pick_strikes(list(by_strike), spot, right, n=6)
        rows = []
        for s in strikes:
            r = by_strike[s]
            rows.append({
                "strike": s,
                "tag": oi.moneyness(s, spot, right),
                "conid": int(r.get("conid") or 0),
                "contract": r,
                "delta": 0.0, "bid": 0.0, "ask": 0.0, "mid": 0.0,
                "credit": None, "yield_pct": None, "ann_pct": None,
            })
        self._chain_view.chain_model.set_rows(rows)
        self._set_status("")
        # Default to the ~3rd row (≈0.30-delta zone) like the reference.
        default_row = 2 if len(rows) >= 3 else 0
        self._price_pending = True
        self._chain_view.select_row(default_row)

        # Subscribe quotes: the six visible strikes + the underlying.
        subs = [dict(r["contract"]) for r in rows]
        if self._underlying_conid > 0:
            subs.append({"conid": self._underlying_conid,
                         "sec_type": "STK", "exchange": "SMART",
                         "currency": "USD"})
        self._worker.request_option_quotes(self._token, subs)

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
            return

        model = self._chain_view.chain_model
        row = model.row_for_conid(conid)
        if row < 0:
            return
        bid, ask, last = q.get("bid", 0.0), q.get("ask", 0.0), q.get("last", 0.0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (last or 0.0)
        rd = model.row_data(row) or {}
        strike = rd.get("strike", 0.0)
        exp = self._expiry.selected()
        dte = exp.dte if exp else 0
        ref = self._spot if self._cards.selected() == "CC" else strike
        mult = self._leg_multiplier(rd)
        patch = {"bid": bid, "ask": ask, "mid": mid,
                 "delta": q.get("delta", 0.0)}
        if mid > 0:
            y = oi.yield_pct(mid, ref)
            patch.update({
                "credit": float(oi.credit(mid, 1, mult)),
                "yield_pct": float(y),
                "ann_pct": float(oi.annualized_pct(y, dte)),
            })
        model.update_row(row, patch)

        if (self._selected_leg is not None
                and self._selected_leg.get("conid") == conid):
            self._rail.set_bid_mid_ask(bid, mid, ask)
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
        self._refresh_cards()
        self._request_contracts()
        self._recompute()

    def _on_expiry_changed(self, _ymd: str) -> None:
        self._price_pending = True
        self._selected_leg = None
        self._request_contracts()

    def _on_row_selected(self, row: int) -> None:
        rd = self._chain_view.chain_model.row_data(row)
        if not rd:
            return
        self._selected_leg = rd
        self._price_pending = True
        bid, mid, ask = rd.get("bid", 0), rd.get("mid", 0), rd.get("ask", 0)
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
        if self._cards.selected() == "CC":
            return oi.cc_capacity(self._shares)
        strike = (self._selected_leg or {}).get("strike") or 0.0
        bp = float(self._get_buying_power(self._account) or 0.0)
        return oi.csp_capacity(bp, strike)

    def _recompute(self) -> None:
        strategy = self._cards.selected()
        status = self._cards.current_status()
        leg = self._selected_leg
        exp = self._expiry.selected()
        max_qty = self._max_qty()
        self._rail.set_max_qty(max_qty)
        qty = self._rail.qty()
        px = self._rail.price()
        mult = self._leg_multiplier()

        if leg and exp:
            strike = leg["strike"]
            word = _RIGHT_WORD[_RIGHT[strategy]]
            self._rail.set_order_line(
                f"SELL {qty or 1} {self._symbol} {exp.chip_label} "
                f"{strike:g} {word} @ {px:.2f} LMT")
        else:
            self._rail.set_order_line("—")

        if leg and px > 0:
            strike = oi.to_dec(leg["strike"])
            if strategy == "CC":
                be = oi.break_even_cc(self._basis, px)
                outcome = oi.if_called_away(
                    strike, self._basis, px, qty, mult)
                self._rail.set_detail(
                    "Break-even", f"${be:,.2f}",
                    "If called away", f"{'+' if outcome >= 0 else ''}"
                                      f"${outcome:,.0f}",
                    f"Covered — {qty * mult} of your "
                    f"{int(self._shares):,} shares back this position; "
                    "they stay in the account unless assigned.")
                reserved = f"Shares reserved {qty * mult} of " \
                           f"{int(self._shares):,}"
            else:
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
            total_credit = oi.credit(px, qty, mult)
            ref = self._spot if strategy == "CC" else float(strike)
            y = oi.yield_pct(px, ref)
            ann = oi.annualized_pct(y, exp.dte if exp else 0)
            self._rail.set_footer(
                f"+${total_credit:,.2f}", reserved,
                f"Ann. {ann:.1f}%" if ann else "")
        else:
            self._rail.set_detail("Break-even", "—",
                                  "If called away" if strategy == "CC"
                                  else "If assigned, cost", "—", "")
            self._rail.set_footer("—", "", "")

        # Sell button state (§4.7): ineligible reason wins, then leg/qty/px.
        if not status.eligible:
            self._rail.set_sell_enabled(False, status.reason)
        elif max_qty <= 0:
            reason = ("Not enough shares" if strategy == "CC"
                      else "Not enough buying power")
            self._rail.set_sell_enabled(False, reason)
        elif leg is None or px <= 0:
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
        max_qty = self._max_qty()
        # Structural clamp again right before transmit (§7).
        if not (0 < qty <= max_qty) or px <= 0:
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
            "action": "SELL",
            "type": "LMT",
            "symbol": contract.get("symbol", self._symbol),
            "qty": int(qty),
            "limit_price": float(px),
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
                 self._cards.selected(), payload)
        self.order_ready.emit(payload)
        self.accept()
