"""
Options Income ticket — right-hand order rail (§3, §4.5–4.7).

One compact top-aligned block inside a fixed-width column: order line,
contracts stepper + MAX, limit price (stepper / typed entry /
Bid-Mid-Ask chips), then Submit order directly beneath.

The rail is a pure view: it owns the qty/price input state and emits
signals; every derived number is pushed in by the ticket.
"""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from src.core.options_income import snap_to_tick, to_dec

ACCENT_BLUE = "#539BF5"

_STEP_BTN_QSS = (
    "QPushButton{ background:#1B2130; color:#E6EBF2; border:none;"
    " border-radius:5px; padding:4px 10px; font-size:12px;"
    " font-weight:bold; }"
    "QPushButton:hover{ background:#2f3442; }"
    "QPushButton:disabled{ color:#6A7690; }")

_CHIP_ON = (
    "QPushButton{ background:rgba(0,113,227,0.16); color:#E6EBF2;"
    f" border:1px solid {ACCENT_BLUE}; border-radius:5px;"
    " padding:4px 8px; font-size:10px; font-weight:bold; }")
_CHIP_OFF = (
    "QPushButton{ background:#1B2130; color:#A3AFC2;"
    f" border:1px solid {Theme.BORDER}; border-radius:5px;"
    " padding:4px 8px; font-size:10px; }"
    "QPushButton:hover{ border-color:#334058; }")

def _caption(parent: QWidget, text: str) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setStyleSheet(
        f"color:{Theme.TEXT_DIM}; font-size:10px; font-weight:bold;"
        " letter-spacing:1px;")
    return lbl


class OrderRail(QWidget):
    qty_changed = Signal(int)
    price_changed = Signal(object)       # Decimal
    side_changed = Signal(str)           # "BUY" | "SELL"
    submit_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # The rail fills the top pane of the ticket's right-hand column
        # (min 300 px wide); an internal bottom stretch keeps the
        # controls hugging the top whatever the pane height.
        self.setMinimumWidth(300)
        self._action = "SELL"      # order side — drives submit button
        self._qty = 1
        self._max_qty = 0
        self._tick = Decimal("0.01")
        self._price = Decimal("0")
        self._bid = Decimal("0")
        self._mid = Decimal("0")
        self._ask = Decimal("0")
        self._anchor = "MID"
        self._market = False       # True → submit as a MKT order
        self.setStyleSheet(
            f"OrderRail{{ background:{Theme.BG_MAIN};"
            f" border:1px solid {Theme.BORDER}; border-radius:8px; }}")
        self._build_ui()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        # ── Zone 1: static header ───────────────────────────────────────
        # ORDER caption with the Buy / Sell side toggle on its right
        # (strategy = side × Calls/Puts toggle).
        caprow = QHBoxLayout()
        caprow.setSpacing(6)
        caprow.addWidget(_caption(self, "ORDER"))
        caprow.addStretch(1)
        self._side_btns: dict[str, QPushButton] = {}
        for key in ("BUY", "SELL"):
            b = QPushButton(key.capitalize(), self)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedWidth(64)
            b.clicked.connect(lambda _c=False, k=key: self._pick_side(k))
            self._side_btns[key] = b
            caprow.addWidget(b)
        outer.addLayout(caprow)
        self._restyle_side()

        self._order_line = QLabel("—", self)
        self._order_line.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:12px; font-weight:bold;")
        self._order_line.setWordWrap(True)
        outer.addWidget(self._order_line)

        # Contracts row
        crow = QHBoxLayout()
        crow.setSpacing(6)
        clabel = QLabel("Contracts", self)
        clabel.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:11px;")
        crow.addWidget(clabel)
        crow.addStretch(1)
        self._qty_minus = QPushButton("−", self)
        self._qty_plus = QPushButton("+", self)
        for b in (self._qty_minus, self._qty_plus):
            b.setStyleSheet(_STEP_BTN_QSS)
            b.setFixedWidth(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._qty_lbl = QLabel("1", self)
        self._qty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qty_lbl.setFixedWidth(36)
        self._qty_lbl.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:13px; font-weight:bold;")
        self._max_btn = QPushButton("MAX 0", self)
        self._max_btn.setStyleSheet(_CHIP_OFF)
        self._max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._qty_minus.clicked.connect(lambda: self._step_qty(-1))
        self._qty_plus.clicked.connect(lambda: self._step_qty(+1))
        self._max_btn.clicked.connect(self._on_max)
        crow.addWidget(self._qty_minus)
        crow.addWidget(self._qty_lbl)
        crow.addWidget(self._qty_plus)
        crow.addWidget(self._max_btn)
        outer.addLayout(crow)

        # Limit price row — stepper + typed entry
        prow = QHBoxLayout()
        prow.setSpacing(6)
        plabel = QLabel("Limit price", self)
        plabel.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:11px;")
        prow.addWidget(plabel)
        prow.addStretch(1)
        self._px_minus = QPushButton("−", self)
        self._px_plus = QPushButton("+", self)
        for b in (self._px_minus, self._px_plus):
            b.setStyleSheet(_STEP_BTN_QSS)
            b.setFixedWidth(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._px_edit = QLineEdit("0.00", self)
        self._px_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._px_edit.setFixedWidth(70)
        validator = QDoubleValidator(0.0, 99999.0, 2, self._px_edit)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._px_edit.setValidator(validator)
        self._px_edit.setStyleSheet(
            f"QLineEdit{{ background:#1B2130; color:{Theme.TEXT};"
            f" border:1px solid {Theme.BORDER}; border-radius:5px;"
            " padding:4px 6px; font-size:13px; font-weight:bold; }"
            f"QLineEdit:focus{{ border-color:{ACCENT_BLUE}; }}")
        self._px_edit.editingFinished.connect(self._on_price_typed)
        self._px_minus.clicked.connect(lambda: self._step_price(-1))
        self._px_plus.clicked.connect(lambda: self._step_price(+1))
        prow.addWidget(self._px_minus)
        prow.addWidget(self._px_edit)
        prow.addWidget(self._px_plus)
        outer.addLayout(prow)

        # Bid / Mid / Ask / Mkt anchor chips
        arow = QHBoxLayout()
        arow.setSpacing(6)
        self._anchor_btns: dict[str, QPushButton] = {}
        for key in ("BID", "MID", "ASK", "MKT"):
            btn = QPushButton(key.capitalize(), self)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._pick_anchor(k))
            self._anchor_btns[key] = btn
            arow.addWidget(btn, stretch=1)
        outer.addLayout(arow)

        # Submit sits right under the price anchors, keeping the whole
        # order flow in one compact block.  (The ticket lives in a
        # permanent tab, so there is nothing to dismiss — no Cancel /
        # Close button; per-order Cancel lives in the Orders panel.)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        self._sell_btn = QPushButton("Submit order", self)
        self._sell_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sell_btn.setStyleSheet(
            f"QPushButton{{ background:{Theme.CLR_SELL}; color:#ffffff;"
            " border:none; border-radius:6px; padding:9px 16px;"
            " font-size:12px; font-weight:bold; }"
            "QPushButton:disabled{ background:#3a3f4d; color:#6A7690; }")
        self._sell_btn.clicked.connect(self.submit_clicked.emit)
        brow.addWidget(self._sell_btn, stretch=1)
        outer.addLayout(brow)
        outer.addStretch(1)
        self._refresh_anchor_chips()

    # ── Qty ─────────────────────────────────────────────────────────────────

    def qty(self) -> int:
        return self._qty

    def set_max_qty(self, n: int) -> None:
        self._max_qty = max(0, int(n))
        self._max_btn.setText(f"MAX {self._max_qty}")
        if self._qty > self._max_qty:
            self._set_qty(self._max_qty)
        elif self._qty == 0 and self._max_qty > 0:
            self._set_qty(1)
        else:
            self._refresh_qty()

    def reset_qty(self) -> None:
        """Snap back to the minimum size (after a submit — the next order
        must be sized deliberately)."""
        self._set_qty(1)

    def _step_qty(self, delta: int) -> None:
        self._set_qty(self._qty + delta)

    def _on_max(self) -> None:
        self._set_qty(self._max_qty)

    def _set_qty(self, n: int) -> None:
        # Structural clamp — over-selling is impossible from the UI (§4.5).
        n = max(0 if self._max_qty == 0 else 1, min(int(n), self._max_qty))
        if n != self._qty:
            self._qty = n
            self._refresh_qty()
            self.qty_changed.emit(n)
        else:
            self._refresh_qty()

    def _refresh_qty(self) -> None:
        self._qty_lbl.setText(str(self._qty))
        self._qty_minus.setEnabled(self._qty > 1)
        self._qty_plus.setEnabled(self._qty < self._max_qty)

    # ── Price ───────────────────────────────────────────────────────────────

    def price(self) -> Decimal:
        return self._price

    def set_tick(self, tick) -> None:
        t = to_dec(tick)
        if t > 0:
            self._tick = t

    def set_price(self, price, *, anchor: str | None = None) -> None:
        """Programmatic price set (leg change / anchor chip)."""
        self._price = snap_to_tick(price, self._tick)
        if anchor is not None:
            self._anchor = anchor
        self._px_edit.setText(f"{self._price:.2f}")
        self._refresh_anchor_chips()
        self.price_changed.emit(self._price)

    def set_bid_mid_ask(self, bid, mid, ask) -> None:
        self._bid, self._mid, self._ask = (
            to_dec(bid), to_dec(mid), to_dec(ask))
        for key, val in (("BID", self._bid), ("MID", self._mid),
                         ("ASK", self._ask)):
            btn = self._anchor_btns[key]
            btn.setText(f"{key.capitalize()} {val:.2f}" if val > 0
                        else key.capitalize())
        self._refresh_anchor_chips()

    def is_market(self) -> bool:
        return self._market

    def _set_market(self, on: bool) -> None:
        """Toggle market mode: the limit-price input greys out and the
        submitted order becomes MKT instead of LMT."""
        if self._market == on:
            self._refresh_anchor_chips()
            return
        self._market = on
        for w in (self._px_minus, self._px_plus, self._px_edit):
            w.setEnabled(not on)
        if on:
            self._anchor = "MKT"
        self._refresh_anchor_chips()
        self.price_changed.emit(self._price)   # host recomputes

    def _step_price(self, direction: int) -> None:
        self._set_market(False)
        step = self._tick if self._tick > 0 else Decimal("0.01")
        new = max(Decimal(0), self._price + step * direction)
        self._anchor = ""
        self.set_price(new)

    def _on_price_typed(self) -> None:
        # Round partial entries like "3." to 2 dp so the math never sees
        # an invalid intermediate (§4.6).
        self._set_market(False)
        try:
            typed = to_dec(self._px_edit.text() or "0")
        except Exception:
            typed = self._price
        self._anchor = ""
        self.set_price(typed)

    def _pick_anchor(self, key: str) -> None:
        if key == "MKT":
            self._set_market(True)
            return
        val = {"BID": self._bid, "MID": self._mid, "ASK": self._ask}[key]
        if val <= 0:
            return
        self._set_market(False)
        self.set_price(val, anchor=key)

    def _refresh_anchor_chips(self) -> None:
        # An anchor chip stays lit while the price still equals its value;
        # market mode wins outright.
        active = ""
        if self._market:
            active = "MKT"
        else:
            current = {"BID": self._bid, "MID": self._mid,
                       "ASK": self._ask}
            for key, val in current.items():
                if val > 0 and snap_to_tick(val, self._tick) == self._price:
                    if self._anchor == key or not active:
                        active = key
        for key, btn in self._anchor_btns.items():
            btn.setStyleSheet(_CHIP_ON if key == active else _CHIP_OFF)

    # ── Derived-value setters (pushed by the ticket) ────────────────────────

    def set_order_line(self, text: str) -> None:
        self._order_line.setText(text)

    def action(self) -> str:
        return self._action

    def _pick_side(self, key: str) -> None:
        if key == self._action:
            return
        self.set_action(key)
        self.side_changed.emit(key)

    def _restyle_side(self) -> None:
        for key, b in self._side_btns.items():
            if key != self._action:
                b.setStyleSheet(_CHIP_OFF)
                continue
            color = Theme.CLR_BUY if key == "BUY" else Theme.CLR_SELL
            b.setStyleSheet(
                f"QPushButton{{ background:{color}; color:#ffffff;"
                " border:none; border-radius:5px; padding:5px 8px;"
                " font-size:11px; font-weight:bold; }")

    def set_action(self, action: str) -> None:
        """"BUY" or "SELL" — flips the submit button colour."""
        action = "BUY" if action == "BUY" else "SELL"
        if action == self._action:
            return
        self._action = action
        self._restyle_side()
        color = Theme.CLR_BUY if action == "BUY" else Theme.CLR_SELL
        self._sell_btn.setStyleSheet(
            f"QPushButton{{ background:{color}; color:#ffffff;"
            " border:none; border-radius:6px; padding:9px 16px;"
            " font-size:12px; font-weight:bold; }"
            "QPushButton:disabled{ background:#3a3f4d; color:#6A7690; }")
        if self._sell_btn.isEnabled():
            self._sell_btn.setText(self._submit_label())

    def _submit_label(self) -> str:
        return "Submit order"

    def set_sell_enabled(self, enabled: bool, reason: str = "") -> None:
        """Disabled state replaces the label with the reason (§4.7)."""
        self._sell_btn.setEnabled(enabled)
        self._sell_btn.setText(self._submit_label() if enabled
                               else (reason or self._submit_label()))
        self._sell_btn.setToolTip("" if enabled else reason)
