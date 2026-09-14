"""
Options Income ticket — right-hand order rail (§3, §4.5–4.7).

One compact top-aligned block inside a fixed-width column, laid out as
an order ticket:

    ORDER
    SELL 1 × SPCX Sep 18 152.5 CALL          (composed header)
    Limit 3.58 · credit $358 · 2.41% / 4d    (derived summary)
    Side        [ Buy | Sell ]               (segmented)
    Contracts   [ − 1 + ]  [MAX 153]         (grouped stepper)
    Limit       [ − 3.58 + ]
    [ Bid 3.55 | Mid 3.58 | Ask 3.60 | Mkt ] (segmented anchors)
    [ Submit order ]

The rail is a pure view: it owns the qty/price input state and emits
signals; every derived number is pushed in by the ticket
(``set_order_line`` for the contract description, ``set_trade_context``
for spot / days-to-expiry feeding the summary line).
"""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QGridLayout,
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

# Segmented control faces (Side toggle + Bid/Mid/Ask/Mkt anchors): flat
# segments on a dark track, the selected one raised slate with white text.
_SEG_ON = (
    "QPushButton{ background:#39435A; color:#FFFFFF; border:none;"
    " border-radius:6px; padding:4px 10px; font-size:12px;"
    " font-weight:bold; }")
_SEG_OFF = (
    "QPushButton{ background:transparent; color:#8B95A7; border:none;"
    " border-radius:6px; padding:4px 10px; font-size:12px; }"
    "QPushButton:hover{ background:#232A3B; }"
    "QPushButton:disabled{ color:#4A5468; }")

# Flat − / + halves inside a grouped stepper box.
_STEP_FLAT = (
    "QPushButton{ background:transparent; color:#E6EBF2; border:none;"
    " font-size:14px; font-weight:bold; padding:2px 10px; }"
    "QPushButton:hover{ background:#232A3B; }"
    "QPushButton:disabled{ color:#4A5468; }")

# One shared shell for every single-height control row: same height,
# same dark fill, same border and radius — the rows read as one system.
_CTRL_H = 32

# MAX — same shell as the stepper boxes; accent border/fill when the
# qty sits at capacity.
_MAX_OFF = (
    "QPushButton{ background:#1B2130; color:#A3AFC2;"
    f" border:1px solid {Theme.BORDER}; border-radius:6px;"
    " padding:4px 10px; font-size:11px; font-weight:600; }"
    "QPushButton:hover{ border-color:#539BF5; }"
    "QPushButton:disabled{ color:#4A5468; }")
_MAX_ON = (
    f"QPushButton{{ background:rgba(0,113,227,0.16); color:#E6EBF2;"
    f" border:1px solid {ACCENT_BLUE}; border-radius:6px;"
    " padding:4px 10px; font-size:11px; font-weight:600; }")

_ROW_LABEL_QSS = f"color:{Theme.TEXT_DIM}; font-size:12px;"

# Dark rounded track that hosts flat segments (Side toggle, anchors).
_TRACK_QSS = (
    "QWidget#railTrack{ background:#1B2130;"
    f" border:1px solid {Theme.BORDER}; border-radius:7px; }}")


class OrderRail(QWidget):
    qty_changed = Signal(int)
    price_changed = Signal(object)       # Decimal
    side_changed = Signal(str)           # "BUY" | "SELL"
    submit_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # The rail fills the top pane of the ticket's right-hand column
        # (min 230 px wide — the content rows set the real floor); an
        # internal bottom stretch keeps the controls hugging the top
        # whatever the pane height.
        self.setMinimumWidth(230)
        self._action = "SELL"      # order side — drives submit button
        self._qty = 1
        self._max_qty = 0
        self._tick = Decimal("0.01")
        self._price = Decimal("0")
        self._bid = Decimal("0")
        self._mid = Decimal("0")
        self._ask = Decimal("0")
        # Sticky order mode — what a submit will actually send:
        #   LMT  → static limit at the typed/stepped price
        #   MKT  → market order
        #   BID / MID / ASK → the price field TRACKS that live quote and
        #   the submit goes out as a broker-side working order (pegged /
        #   adaptive; the ticket maps mode × side to the payload type).
        # Picking a strike arms MID by default (the ticket seeds the mid
        # via set_price(..., anchor="MID")); typing or stepping the
        # price demotes to plain LMT.
        self._mode = "LMT"
        self._desc = ""            # contract description (ticket-pushed)
        self._spot = 0.0           # underlying last (ticket-pushed)
        self._dte = 0              # days to expiry (ticket-pushed)
        self._n_accounts = 1       # checked accounts (ticket-pushed)
        self._wheel_accum: dict[int, int] = {}
        self.setStyleSheet(
            f"OrderRail{{ background:{Theme.BG_MAIN};"
            f" border:1px solid {Theme.BORDER}; border-radius:8px; }}")
        self._build_ui()

    # ── UI construction ─────────────────────────────────────────────────────

    def _stepper_box(self, minus: QPushButton, edit: QLineEdit,
                     plus: QPushButton) -> QWidget:
        """[ − | value | + ] as ONE rounded control: the box carries the
        background/border, the halves sit flat inside it; the value
        stretches so the box can fill its row."""
        box = QWidget(self)
        box.setObjectName("stepBox")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setFixedHeight(_CTRL_H)
        box.setStyleSheet(
            "QWidget#stepBox{ background:#1B2130;"
            f" border:1px solid {Theme.BORDER}; border-radius:6px; }}")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        for b in (minus, plus):
            b.setStyleSheet(_STEP_FLAT)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedWidth(30)
        edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        edit.setStyleSheet(
            f"QLineEdit{{ background:transparent; color:{Theme.TEXT};"
            " border:none; font-size:12px; font-weight:bold;"
            " font-family:'JetBrains Mono'; }"
            "QLineEdit:disabled{ color:#4A5468; }")
        lay.addWidget(minus)
        lay.addWidget(edit, 1)
        lay.addWidget(plus)
        return box

    def _track(self) -> tuple[QWidget, QHBoxLayout]:
        """Dark rounded track hosting flat segments — the Side toggle
        and the anchor row share it so every row wears the same shell."""
        track = QWidget(self)
        track.setObjectName("railTrack")
        track.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        track.setStyleSheet(_TRACK_QSS)
        lay = QHBoxLayout(track)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        return track, lay

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # ── Header: composed order line ────────────────────────────────
        self._order_line = QLabel("—", self)
        self._order_line.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:13px; font-weight:bold;")
        self._order_line.setWordWrap(True)
        outer.addWidget(self._order_line)
        outer.addSpacing(6)

        # ── Side / Qty / Limit as a form grid: labels in column 0, all
        # controls sharing one left-aligned edge in column 1 ────────────
        form = QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        slabel = QLabel("Side", self)
        slabel.setStyleSheet(_ROW_LABEL_QSS)
        form.addWidget(slabel, 0, 0)
        strack, srow = self._track()
        strack.setFixedHeight(_CTRL_H)
        self._side_btns: dict[str, QPushButton] = {}
        for key in ("BUY", "SELL"):
            b = QPushButton(key.capitalize(), self)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self._pick_side(k))
            self._side_btns[key] = b
            srow.addWidget(b, 1)
        form.addWidget(strack, 0, 1)
        self._restyle_side()

        clabel = QLabel("Qty", self)
        clabel.setStyleSheet(_ROW_LABEL_QSS)
        form.addWidget(clabel, 1, 0)
        crow = QHBoxLayout()
        crow.setSpacing(6)
        self._qty_minus = QPushButton("−", self)
        self._qty_plus = QPushButton("+", self)
        self._qty_edit = QLineEdit("1", self)
        self._qty_edit.setValidator(QIntValidator(0, 9999, self._qty_edit))
        self._qty_edit.editingFinished.connect(self._on_qty_typed)
        self._qty_edit.installEventFilter(self)
        self._qty_minus.clicked.connect(lambda: self._step_qty(-1))
        self._qty_plus.clicked.connect(lambda: self._step_qty(+1))
        crow.addWidget(self._stepper_box(
            self._qty_minus, self._qty_edit, self._qty_plus), 1)
        self._max_btn = QPushButton("MAX 0", self)
        self._max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._max_btn.setFixedHeight(_CTRL_H)
        self._max_btn.clicked.connect(self._on_max)
        crow.addWidget(self._max_btn)
        form.addLayout(crow, 1, 1)

        plabel = QLabel("Price", self)
        plabel.setStyleSheet(_ROW_LABEL_QSS)
        form.addWidget(plabel, 2, 0)
        self._px_minus = QPushButton("−", self)
        self._px_plus = QPushButton("+", self)
        self._px_edit = QLineEdit("0.00", self)
        validator = QDoubleValidator(0.0, 99999.0, 2, self._px_edit)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._px_edit.setValidator(validator)
        self._px_edit.editingFinished.connect(self._on_price_typed)
        self._px_edit.installEventFilter(self)
        self._px_minus.clicked.connect(lambda: self._step_price(-1))
        self._px_plus.clicked.connect(lambda: self._step_price(+1))
        form.addWidget(self._stepper_box(
            self._px_minus, self._px_edit, self._px_plus), 2, 1)

        form.setColumnStretch(1, 1)
        outer.addLayout(form)

        # ── Anchor segments: Bid / Mid / Ask two-line, Mkt ──────────────
        atrack, arow = self._track()
        self._anchor_btns: dict[str, QPushButton] = {}
        for key in ("BID", "MID", "ASK", "MKT"):
            btn = QPushButton(key.capitalize(), self)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Let the segments compress below their two-line text hint —
            # the stretch keeps all four equal at whatever width the
            # pane has.
            btn.setMinimumWidth(40)
            btn.clicked.connect(lambda _c=False, k=key: self._pick_anchor(k))
            self._anchor_btns[key] = btn
            arow.addWidget(btn, stretch=1)
        outer.addWidget(atrack)

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
        self._refresh_qty()
        self._refresh_anchor_chips()

    # ── Header (ORDER line + summary) ───────────────────────────────────────

    def set_order_line(self, text: str) -> None:
        """Contract description from the ticket ("SPCX Sep 18 152.5
        CALL"); "—" / empty clears the header."""
        text = (text or "").strip()
        self._desc = "" if text in ("", "—") else text
        self._refresh_header()

    def set_trade_context(self, spot: float, dte: int,
                          n_accounts: int = 1) -> None:
        """Underlying last + days-to-expiry + checked-account count —
        feeds the summary line's yield% / dte readout and the header's
        total contract count (qty fans out to every checked account)."""
        self._spot = float(spot or 0.0)
        self._dte = max(0, int(dte or 0))
        self._n_accounts = max(1, int(n_accounts or 1))
        self._refresh_header()

    def _refresh_header(self) -> None:
        if not self._desc:
            self._order_line.setText("—")
            return
        # The same qty goes to EVERY checked account — the header shows
        # the total the submit will actually place.
        contracts = self._qty * self._n_accounts
        self._order_line.setText(
            f"{self._action} {contracts} × {self._desc}")

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

    def _on_max(self) -> None:
        self._set_qty(self._max_qty)

    def _step_qty(self, delta: int) -> None:
        self._set_qty(self._qty + delta)

    def _on_qty_typed(self) -> None:
        try:
            n = int(self._qty_edit.text())
        except ValueError:
            self._refresh_qty()
            return
        self._set_qty(n)

    def _set_qty(self, n: int) -> None:
        # Structural clamp — over-selling is impossible from the UI (§4.5).
        n = max(0 if self._max_qty == 0 else 1, min(int(n), self._max_qty))
        if n != self._qty:
            self._qty = n
            self._refresh_qty()
            self.qty_changed.emit(n)
        else:
            self._refresh_qty()
        self._refresh_header()

    def _refresh_qty(self) -> None:
        self._qty_edit.setText(str(self._qty))
        self._qty_minus.setEnabled(self._qty > 1)
        self._qty_plus.setEnabled(self._qty < self._max_qty)
        self._max_btn.setEnabled(self._max_qty > 0)
        self._max_btn.setStyleSheet(
            _MAX_ON if self._max_qty > 0 and self._qty == self._max_qty
            else _MAX_OFF)

    # ── Price ───────────────────────────────────────────────────────────────

    def price(self) -> Decimal:
        return self._price

    def set_tick(self, tick) -> None:
        t = to_dec(tick)
        if t > 0:
            self._tick = t

    def set_price(self, price, *, anchor: str | None = None) -> None:
        """Programmatic price set (leg change / anchor chip).  *anchor*
        also arms that tracking mode (the ticket's ``anchor="MID"`` on
        leg selection is what makes Mid the default working order)."""
        self._price = snap_to_tick(price, self._tick)
        if anchor is not None:
            self._set_mode(anchor)
        self._px_edit.setText(f"{self._price:.2f}")
        self._refresh_anchor_chips()
        self._refresh_header()
        self.price_changed.emit(self._price)

    def set_bid_mid_ask(self, bid, mid, ask) -> None:
        self._bid, self._mid, self._ask = (
            to_dec(bid), to_dec(mid), to_dec(ask))
        for key, val in (("BID", self._bid), ("MID", self._mid),
                         ("ASK", self._ask)):
            btn = self._anchor_btns[key]
            # Snap the label with the SAME tick as the price field —
            # a raw mid of 7.365 must not read "7.36" under a field
            # showing the snapped 7.37.
            disp = snap_to_tick(val, self._tick)
            btn.setText(f"{key.capitalize()}\n{disp:.2f}" if val > 0
                        else key.capitalize())
        # An armed tracking mode follows the live quote — the field
        # shows what the working order's limit/cap will be at submit.
        if self._mode in ("BID", "MID", "ASK"):
            val = {"BID": self._bid, "MID": self._mid,
                   "ASK": self._ask}[self._mode]
            if val > 0:
                snapped = snap_to_tick(val, self._tick)
                if snapped != self._price:
                    self._price = snapped
                    self._px_edit.setText(f"{self._price:.2f}")
                    self._refresh_header()
                    self.price_changed.emit(self._price)
        self._refresh_anchor_chips()

    def order_mode(self) -> str:
        """"LMT" | "MKT" | "BID" | "MID" | "ASK" — what Submit sends."""
        return self._mode

    def is_market(self) -> bool:
        return self._mode == "MKT"

    def _set_mode(self, mode: str) -> None:
        """Central mode transition: gates the price field (market mode
        greys it out) and re-lights the anchor segments."""
        if mode == self._mode:
            self._refresh_anchor_chips()
            return
        self._mode = mode
        enable_px = mode != "MKT"
        for w in (self._px_minus, self._px_plus, self._px_edit):
            w.setEnabled(enable_px)
        self._refresh_anchor_chips()
        self._refresh_header()
        self.price_changed.emit(self._price)   # host recomputes gating

    def eventFilter(self, obj, ev):  # noqa: N802 (Qt override)
        # Wheel / ↑↓ step the price by one tick and the qty by one.
        # getattr: events already flow while _build_ui is mid-way, when
        # the qty edit exists but the price edit doesn't yet.
        px_edit = getattr(self, "_px_edit", None)
        if obj is px_edit or obj is getattr(self, "_qty_edit", None):
            is_px = obj is px_edit
            step = self._step_price if is_px else self._step_qty
            commit = self._on_price_typed if is_px else self._on_qty_typed
            t = ev.type()
            if t == QEvent.Type.Wheel:
                acc = self._wheel_accum.get(id(obj), 0)
                acc += ev.angleDelta().y()
                while acc >= 120:
                    acc -= 120
                    step(+1)
                while acc <= -120:
                    acc += 120
                    step(-1)
                self._wheel_accum[id(obj)] = acc
                return True
            if (t == QEvent.Type.KeyPress
                    and ev.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
                # Commit any half-typed value first so the step applies
                # to what the user sees, not the last committed value.
                commit()
                step(+1 if ev.key() == Qt.Key.Key_Up else -1)
                return True
        return super().eventFilter(obj, ev)

    def _step_price(self, direction: int) -> None:
        # Taking manual control of the price demotes to a plain limit.
        self._set_mode("LMT")
        step = self._tick if self._tick > 0 else Decimal("0.01")
        new = max(Decimal(0), self._price + step * direction)
        self.set_price(new)

    def _on_price_typed(self) -> None:
        # Round partial entries like "3." to 2 dp so the math never sees
        # an invalid intermediate (§4.6).  Typing a price demotes the
        # armed tracking mode to a plain limit.
        self._set_mode("LMT")
        try:
            typed = to_dec(self._px_edit.text() or "0")
        except Exception:
            typed = self._price
        self.set_price(typed)

    def _pick_anchor(self, key: str) -> None:
        if key == "MKT":
            self._set_mode("MKT")
            return
        val = {"BID": self._bid, "MID": self._mid, "ASK": self._ask}[key]
        if val <= 0:
            return
        self.set_price(val, anchor=key)

    def _refresh_anchor_chips(self) -> None:
        # The lit segment IS the order mode (sticky) — not a
        # price-equality echo.
        for key, btn in self._anchor_btns.items():
            btn.setStyleSheet(_SEG_ON if key == self._mode else _SEG_OFF)

    # ── Side / submit ───────────────────────────────────────────────────────

    def action(self) -> str:
        return self._action

    def _pick_side(self, key: str) -> None:
        if key == self._action:
            return
        self.set_action(key)
        self.side_changed.emit(key)

    def _restyle_side(self) -> None:
        # The selected side wears the app-wide Buy green / Sell red (the
        # order-entry buttons' colours); the idle side stays a flat
        # segment.
        for key, b in self._side_btns.items():
            if key != self._action:
                b.setStyleSheet(_SEG_OFF)
                continue
            color = Theme.CLR_BUY if key == "BUY" else Theme.CLR_SELL
            b.setStyleSheet(
                f"QPushButton{{ background:{color}; color:#ffffff;"
                " border:none; border-radius:6px; padding:4px 10px;"
                " font-size:12px; font-weight:bold; }")

    def set_action(self, action: str) -> None:
        """"BUY" or "SELL" — flips the submit button colour."""
        action = "BUY" if action == "BUY" else "SELL"
        if action == self._action:
            return
        self._action = action
        self._restyle_side()
        self._refresh_header()
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
