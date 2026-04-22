"""Depth-of-Market price ladder for the Book Trader.

Three classes:

* ``DOMLadderModel`` — ``QAbstractTableModel`` holding the ladder state
  (list of prices spaced by tick size around a reference price, and
  ``price → size`` dicts for the bid and ask sides) plus the last
  traded price and its up/down/flat tick direction.
* ``DOMLadderDelegate`` — ``QStyledItemDelegate`` that paints each cell
  with a proportional depth bar (size / max_visible_size) on the inner
  edge of the size columns, and highlights the last-price row on the
  price column.  All painting is bespoke so the render cost is a
  single ``painter.fillRect`` + ``drawText`` per cell.
* ``DOMLadderView`` — thin ``QTableView`` subclass that hides both
  headers, pins a fixed row height, disables selection, and overrides
  ``mousePressEvent`` so clicking a bid-size cell emits
  ``ladder_clicked("BUY", price)`` and an ask-size cell emits
  ``ladder_clicked("SELL", price)``.

All three together are fed by ``BookTraderView`` which subscribes the
worker to ``reqMktDepth`` and relays ``depth_updated`` batches through
``DOMLadderModel.feed_depth`` on the 100 ms throttle tick.  ``feed_last``
is driven by the L1 ``bg_price_updated`` signal.
"""
from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QStyledItemDelegate,
    QTableView,
)

from src.ui.theme import Theme


# ── Column layout ─────────────────────────────────────────────────────────────

COL_BID_SZ = 0
COL_PRICE = 1
COL_ASK_SZ = 2
_N_COLS = 3

# Default number of ladder rows (symmetric around the last price).  121
# rows = 60 above + last + 60 below.  Most layouts can't show all 121
# rows at once; the view scrolls via mouse-wheel/trackpad (scrollbar is
# hidden by policy) and auto-centres on the last-price row after a
# recenter.
_DEFAULT_ROWS_EACH_SIDE = 60


# ── Custom data roles ────────────────────────────────────────────────────────
# Using int values in the QAbstractTableModel.UserRole range so they don't
# collide with Qt built-ins.

SizeRole = Qt.ItemDataRole.UserRole + 1        # float size at this row
MaxSizeRole = Qt.ItemDataRole.UserRole + 2     # float max size across ladder
IsLastRole = Qt.ItemDataRole.UserRole + 3      # bool — this is the last price
TickDirRole = Qt.ItemDataRole.UserRole + 4     # "up" / "down" / "flat"
PriceRole = Qt.ItemDataRole.UserRole + 5       # float price at this row
AboveLastRole = Qt.ItemDataRole.UserRole + 6   # bool — price > last
OrdersRole = Qt.ItemDataRole.UserRole + 7      # list[dict]: open orders at
                                               # this price (dicts carry
                                               # action/qty/type keys)
LastTradeSizeRole = Qt.ItemDataRole.UserRole + 8   # float — size of the most
                                                   # recent print when its
                                                   # price matches this row,
                                                   # 0 elsewhere.  Drives the
                                                   # TWS-style "{size} @ {price}"
                                                   # stripe in the delegate.


# ── Colours (blended with Theme) ──────────────────────────────────────────────

_BID_BAR_ALPHA = 70        # out of 255 — translucent depth fill
_ASK_BAR_ALPHA = 70
# Subtle last-price-row highlight — a touch lighter than BG_PANEL so the
# row reads as a single band without shouting.  Matches the Book Trader
# mockup where the last row is a near-imperceptible lift with a green
# price + arrow glyph on top.
_LAST_ROW_BG = QColor("#242834")
_LAST_ROW_EDGE = QColor("#2F3547")
# TWS-style muted blue for the last-trade stripe.  Slightly lighter than
# _LAST_ROW_BG so the band reads as a distinct, temporary highlight.
_LAST_TRADE_STRIPE = QColor("#2a4d6d")


# ══════════════════════════════════════════════════════════════════════════════
#  Model
# ══════════════════════════════════════════════════════════════════════════════

class DOMLadderModel(QAbstractTableModel):
    """In-memory price-ladder state with O(1) per-cell reads.

    The ladder is a fixed list of prices spaced by ``_tick`` around a
    reference price.  ``feed_depth`` replaces both size dicts wholesale
    (ib_async's ``domBids`` / ``domAsks`` is the full current state, not
    an incremental diff).  ``feed_last`` updates the last-price indicator
    without touching the size dicts.  ``recenter`` rebuilds the ladder
    prices when the last drifts too far from the ladder center.
    """

    # Auto-recenter when the last price drifts more than this many
    # rows from the ladder's centre — but only after the drift has
    # persisted for ``_RECENTER_DEBOUNCE_MS`` so the user has a chance
    # to click a row far from last without the grid yanking away.
    _RECENTER_DRIFT_ROWS = 10
    _RECENTER_DEBOUNCE_MS = 3000

    def __init__(
        self,
        parent=None,
        *,
        rows_each_side: int = _DEFAULT_ROWS_EACH_SIDE,
        tick_size: float = 0.25,
    ) -> None:
        super().__init__(parent)
        self._rows_each_side: int = max(1, rows_each_side)
        self._tick: float = float(tick_size) if tick_size > 0 else 0.25
        self._prices: list[float] = []
        self._bid: dict[float, float] = {}
        self._ask: dict[float, float] = {}
        self._last: float = 0.0
        self._prev_last: float = 0.0
        self._last_dir: Literal["up", "down", "flat"] = "flat"
        self._max_size: float = 0.0
        # Open orders grouped by (snapped) price → list of dicts of the
        # form {"action": "BUY"/"SELL", "qty": int, "type": str}.  Fed
        # by BookTraderView via ``set_orders``.
        self._orders: dict[float, list[dict]] = {}
        # Most recent print — drives the TWS-style last-trade stripe on
        # the PRICE column.  Zero means "no stripe".
        self._last_trade_price: float = 0.0
        self._last_trade_size: float = 0.0
        # Start with a zero-centred ladder so rowCount() > 0 before the
        # first market data tick (prevents a blank view during warmup).
        self._rebuild_prices(0.0)
        # Debounce timer for auto-recenter.  Starts when drift first
        # exceeds _RECENTER_DRIFT_ROWS; if still drifted when it fires,
        # the ladder rebuilds around the current last.  Cancelled if the
        # market walks back inside the threshold before the timeout.
        self._recenter_timer = QTimer(self)
        self._recenter_timer.setSingleShot(True)
        self._recenter_timer.setInterval(self._RECENTER_DEBOUNCE_MS)
        self._recenter_timer.timeout.connect(self._on_recenter_timeout)

    # ── Qt model plumbing ─────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._prices)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return _N_COLS

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self._prices):
            return None
        price = self._prices[row]

        if role == PriceRole:
            return price
        if role == SizeRole:
            if col == COL_BID_SZ:
                return float(self._bid.get(price, 0.0))
            if col == COL_ASK_SZ:
                return float(self._ask.get(price, 0.0))
            return None
        if role == MaxSizeRole:
            return self._max_size
        if role == IsLastRole:
            return self._last > 0 and abs(price - self._last) < self._tick / 2
        if role == AboveLastRole:
            return self._last > 0 and price > self._last
        if role == TickDirRole:
            return self._last_dir
        if role == OrdersRole:
            return self._orders.get(price, [])
        if role == LastTradeSizeRole:
            if (col == COL_PRICE and self._last_trade_size > 0
                    and self._last_trade_price > 0
                    and abs(price - self._last_trade_price) < self._tick / 2):
                return float(self._last_trade_size)
            return 0.0
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_PRICE:
                return f"{price:,.2f}"
            if col == COL_BID_SZ:
                sz = self._bid.get(price, 0.0)
                return f"{int(sz):,}" if sz else ""
            if col == COL_ASK_SZ:
                sz = self._ask.get(price, 0.0)
                return f"{int(sz):,}" if sz else ""
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def feed_depth(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        """Replace both side dicts from a fresh depth snapshot.

        Prices are snapped to the ladder's tick grid; out-of-ladder
        levels are still stored (so a subsequent ``recenter`` picks
        them up without a round-trip), but only in-ladder rows emit
        ``dataChanged`` here.
        """
        self._bid.clear()
        self._ask.clear()
        max_sz = 0.0
        tick = self._tick
        for price, size in bids:
            if tick > 0:
                p = round(round(price / tick) * tick, 10)
            else:
                p = float(price)
            self._bid[p] = float(size)
            if size > max_sz:
                max_sz = float(size)
        for price, size in asks:
            if tick > 0:
                p = round(round(price / tick) * tick, 10)
            else:
                p = float(price)
            self._ask[p] = float(size)
            if size > max_sz:
                max_sz = float(size)
        self._max_size = max_sz
        # Cheap global repaint — _rows_each_side*2+1 cells.
        n = self.rowCount()
        if n:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(n - 1, _N_COLS - 1),
                [Qt.ItemDataRole.DisplayRole, SizeRole, MaxSizeRole],
            )

    def set_orders(self, orders: list[dict]) -> None:
        """Replace the open-order overlay.

        *orders* is a list of dicts of the shape
        ``{"action": "BUY"/"SELL", "qty": int, "price": float,
           "type": "LMT"/"STP"/"STP LMT"}``.  Prices are snapped to the
        ladder grid; orders that fall on the same snapped price are
        grouped together (the delegate sums qtys for the badge label).
        """
        self._orders.clear()
        tick = self._tick
        for o in orders or []:
            try:
                price = float(o.get("price", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            if tick > 0:
                price = round(round(price / tick) * tick, 10)
            self._orders.setdefault(price, []).append({
                "action": o.get("action", ""),
                "qty": int(o.get("qty", 0) or 0),
                "type": o.get("type", ""),
            })
        n = self.rowCount()
        if n:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(n - 1, _N_COLS - 1),
                [OrdersRole],
            )

    def feed_last(self, price: float) -> None:
        """Update the last-traded price highlight and its tick direction."""
        price = float(price or 0.0)
        if price <= 0:
            return
        if self._last > 0:
            if price > self._last:
                self._last_dir = "up"
            elif price < self._last:
                self._last_dir = "down"
            else:
                # No change — leave dir alone so the arrow persists
                # rather than flipping to "flat" on every identical tick.
                pass
        self._prev_last = self._last
        self._last = price

        # Debounced auto-rebuild: start a single-shot timer when drift
        # first exceeds the threshold; if still drifted when it fires,
        # recenter.  Cancel on recovery.  The delay gives the user a
        # window to click a distant row without the grid yanking away.
        drift_rows = self._drift_from_centre(price)
        if drift_rows > self._RECENTER_DRIFT_ROWS:
            if not self._recenter_timer.isActive():
                self._recenter_timer.start()
        elif self._recenter_timer.isActive():
            self._recenter_timer.stop()

        # Narrow emit over the whole price column plus the two size
        # columns (so the above/below colour may change on cells that
        # crossed the new last price).
        n = self.rowCount()
        if n:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(n - 1, _N_COLS - 1),
                [IsLastRole, AboveLastRole, TickDirRole],
            )

    def feed_last_trade(self, price: float, size: float) -> None:
        """Record the most recent print.  The delegate paints a TWS-style
        ``{size} @ {price}`` stripe on the PRICE cell of the matching
        row; non-positive ``price`` or ``size`` is ignored."""
        try:
            price = float(price or 0.0)
            size = float(size or 0.0)
        except (TypeError, ValueError):
            return
        if price <= 0 or size <= 0:
            return
        tick = self._tick if self._tick > 0 else 0.01
        snapped = round(round(price / tick) * tick, 10)
        old_row = self._row_for_price(self._last_trade_price)
        self._last_trade_price = snapped
        self._last_trade_size = size
        new_row = self._row_for_price(snapped)
        for r in {old_row, new_row}:
            if r is None:
                continue
            idx = self.index(r, COL_PRICE)
            self.dataChanged.emit(
                idx, idx,
                [LastTradeSizeRole, Qt.ItemDataRole.DisplayRole],
            )

    def _row_for_price(self, price: float) -> int | None:
        """Return the row index whose ladder price matches *price* within
        half a tick, or ``None`` if not on the ladder."""
        if price <= 0 or not self._prices:
            return None
        tol = self._tick / 2 if self._tick > 0 else 1e-9
        for i, p in enumerate(self._prices):
            if abs(p - price) < tol:
                return i
        return None

    def recenter(self, price: float) -> None:
        """Rebuild the ladder prices centred on *price*.  Wipes size
        dicts — caller should re-feed on the next depth tick."""
        # A manual recenter (or one from outside feed_last) supersedes
        # any pending debounced auto-recenter.
        if self._recenter_timer.isActive():
            self._recenter_timer.stop()
        self.beginResetModel()
        self._rebuild_prices(price)
        self._bid.clear()
        self._ask.clear()
        self._max_size = 0.0
        self.endResetModel()

    def _drift_from_centre(self, price: float) -> float:
        """Rows between *price* and the ladder's current centre price."""
        if not self._prices or self._tick <= 0 or price <= 0:
            return 0.0
        mid_price = self._prices[len(self._prices) // 2]
        return abs(price - mid_price) / self._tick

    def _on_recenter_timeout(self) -> None:
        """Debounce fired — recenter only if the last is still drifted."""
        if self._last <= 0:
            return
        if self._drift_from_centre(self._last) > self._RECENTER_DRIFT_ROWS:
            self.recenter(self._last)

    def set_tick_size(self, tick: float) -> None:
        tick = float(tick)
        if tick <= 0 or tick == self._tick:
            return
        self._tick = tick
        self.recenter(self._last if self._last > 0 else 0.0)

    @property
    def prices(self) -> list[float]:
        return self._prices

    @property
    def tick_size(self) -> float:
        return self._tick

    @property
    def last_price(self) -> float:
        return self._last

    # ── Internal ──────────────────────────────────────────────────────────────

    def _rebuild_prices(self, centre: float) -> None:
        """Build a descending list of ladder prices around *centre*."""
        tick = self._tick
        n = self._rows_each_side
        if tick <= 0:
            self._prices = [0.0] * (2 * n + 1)
            return
        snapped = round(round(centre / tick) * tick, 10) if centre else 0.0
        out: list[float] = []
        for i in range(n, -n - 1, -1):          # highest price first
            out.append(round(snapped + i * tick, 10))
        self._prices = out


# ══════════════════════════════════════════════════════════════════════════════
#  Delegate
# ══════════════════════════════════════════════════════════════════════════════

class DOMLadderDelegate(QStyledItemDelegate):
    """Custom-paints size cells with proportional depth bars and the
    price cell with above/below-last colouring plus a last-row highlight
    and tick-direction glyph."""

    _ROW_H = 16
    _NUMERIC_FONT = QFont("Menlo, Consolas, monospace", 9)
    _PRICE_FONT = QFont("Menlo, Consolas, monospace", 10)
    _PRICE_FONT.setWeight(QFont.Weight.Bold)
    _LAST_ARROW_FONT = QFont("Helvetica Neue, Arial", 9)
    _LAST_ARROW_FONT.setWeight(QFont.Weight.Bold)
    _TRADE_STRIPE_FONT = QFont("Helvetica Neue, Arial", 9)
    _TRADE_STRIPE_FONT.setWeight(QFont.Weight.Bold)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            rect = option.rect
            col = index.column()
            is_last = bool(index.data(IsLastRole))
            above_last = bool(index.data(AboveLastRole))

            # Row background — highlight the last-price row across every
            # column so it reads as a single bar.
            if is_last:
                painter.fillRect(rect, _LAST_ROW_BG)
            else:
                painter.fillRect(rect, QColor(Theme.BG_PANEL))

            if col == COL_BID_SZ:
                self._paint_size(painter, rect, index, is_bid=True)
            elif col == COL_ASK_SZ:
                self._paint_size(painter, rect, index, is_bid=False)
            elif col == COL_PRICE:
                self._paint_price(painter, rect, index,
                                  is_last=is_last, above_last=above_last)
        finally:
            painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        sh = super().sizeHint(option, index)
        sh.setHeight(self._ROW_H)
        return sh

    # ── Cell painters ─────────────────────────────────────────────────────────

    def _paint_size(self, painter, rect, index, *, is_bid: bool) -> None:
        """Paint a bid-size or ask-size cell.

        Depth bar + size numeric honour the zone rule (bid only below
        last, ask only above last — the opposite side's depth data is
        effectively 0 anyway).  The open-order badge bypasses that
        rule so the user still sees markers for stop-buys above last
        or stop-sells below last.
        """
        is_last = bool(index.data(IsLastRole))
        above_last = bool(index.data(AboveLastRole))

        draw_depth = (
            not is_last
            and not (is_bid and above_last)
            and not ((not is_bid) and (not above_last))
        )

        if draw_depth:
            size = float(index.data(SizeRole) or 0.0)
            max_size = float(index.data(MaxSizeRole) or 0.0)
            if size > 0 and max_size > 0:
                frac = max(0.02, min(1.0, size / max_size))
                bar_w = int(rect.width() * frac)
                if is_bid:
                    # Bid bar: anchor to the RIGHT (inner, next to the
                    # price column) edge and grow leftward toward the
                    # outer edge in proportion to size.
                    bar = rect.adjusted(rect.width() - bar_w, 2, 0, -2)
                    col = QColor(Theme.CLR_BUY)
                    col.setAlpha(_BID_BAR_ALPHA)
                else:
                    # Ask bar: anchor to the LEFT (inner) edge of the
                    # ask column and grow rightward toward the outer
                    # edge.
                    bar = rect.adjusted(0, 2, -(rect.width() - bar_w), -2)
                    col = QColor(Theme.CLR_SELL)
                    col.setAlpha(_ASK_BAR_ALPHA)
                painter.fillRect(bar, col)

            # Numeric text.  Bid LEFT-aligned (outward), ask RIGHT-
            # aligned (outward) — size numerals hug the outer edge
            # while the depth bar fills from the inner edge.
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            if text:
                painter.setFont(self._NUMERIC_FONT)
                painter.setPen(QColor(Theme.TEXT))
                flags = (Qt.AlignmentFlag.AlignVCenter |
                         (Qt.AlignmentFlag.AlignLeft if is_bid
                          else Qt.AlignmentFlag.AlignRight))
                painter.drawText(rect.adjusted(6, 0, -6, 0),
                                 int(flags), text)

        # Order badge — show a "B <qty>" / "S <qty>" pill at the inner
        # edge whenever the user has a live order at this price on the
        # matching side.  Drawn regardless of zone so stop-buys above
        # last and stop-sells below last are still visible.
        if not is_last:
            orders = index.data(OrdersRole) or []
            wanted = "BUY" if is_bid else "SELL"
            side_orders = [o for o in orders
                           if o.get("action") == wanted]
            if side_orders:
                total_qty = sum(int(o.get("qty", 0))
                                for o in side_orders)
                if total_qty > 0:
                    self._paint_order_badge(
                        painter, rect, is_bid=is_bid, qty=total_qty,
                        order_types={o.get("type", "")
                                     for o in side_orders})

    def _paint_order_badge(self, painter, rect, *, is_bid: bool,
                            qty: int, order_types: set[str]) -> None:
        """Draw a small pill indicating the user has live orders at this
        price.  Pill sits at the INNER edge of the size cell (next to
        the price column) so badges on both sides frame the price row.

        Stop-limit orders get an outlined pill (hollow) to distinguish
        them from plain limit/market-qty orders which get a solid fill.
        """
        is_stop = any("STP" in t for t in order_types)
        badge_w = 36
        badge_h = rect.height() - 6
        if is_bid:
            # Inner edge of bid cell = right side
            x = rect.right() - badge_w - 2
        else:
            # Inner edge of ask cell = left side
            x = rect.left() + 2
        y = rect.top() + 3
        from PySide6.QtCore import QRect
        badge = QRect(x, y, badge_w, badge_h)

        colour = QColor(Theme.CLR_BUY if is_bid else Theme.CLR_SELL)
        if is_stop:
            # Hollow outline for stop / stop-limit
            painter.setPen(colour)
            painter.setBrush(QColor(0, 0, 0, 0))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(colour)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("white"))

        painter.setFont(self._NUMERIC_FONT)
        label = f"{'B' if is_bid else 'S'} {qty}"
        painter.drawText(
            badge,
            int(Qt.AlignmentFlag.AlignVCenter
                | Qt.AlignmentFlag.AlignHCenter),
            label,
        )
        # Reset the brush so subsequent cells aren't tinted.
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_price(self, painter, rect, index, *,
                     is_last: bool, above_last: bool) -> None:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if not text:
            return

        # Last-trade stripe — when the most recent print snaps onto this
        # row, paint a TWS-style "{size} @ {price}" band across the
        # price cell (replacing the normal price label for this row).
        trade_sz = float(index.data(LastTradeSizeRole) or 0.0)
        if trade_sz > 0:
            price_val = float(index.data(PriceRole) or 0.0)
            painter.fillRect(rect, _LAST_TRADE_STRIPE)
            painter.setFont(self._TRADE_STRIPE_FONT)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignHCenter),
                f"{int(trade_sz):,} @ {price_val:,.2f}",
            )
            return

        painter.setFont(self._PRICE_FONT)
        if is_last:
            # Last-price row: coloured green (prints the last-traded
            # price the trader is reacting to) with a tick-direction
            # arrow glued to the right of the price — both centred as
            # a single unit so "338.56 ▲" reads as one label.
            direction = index.data(TickDirRole) or "flat"
            if direction == "up":
                colour = QColor(Theme.CLR_BUY)
                arrow = " ▲"
            elif direction == "down":
                colour = QColor(Theme.CLR_SELL)
                arrow = " ▼"
            else:
                colour = QColor(Theme.CLR_BUY)
                arrow = ""
            painter.setPen(colour)
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignHCenter),
                text + arrow,
            )
        else:
            # Red above last (asks zone), green below (bids zone).
            painter.setPen(
                QColor(Theme.CLR_SELL) if above_last
                else QColor(Theme.CLR_BUY))
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignVCenter
                    | Qt.AlignmentFlag.AlignHCenter),
                text,
            )


# ══════════════════════════════════════════════════════════════════════════════
#  View
# ══════════════════════════════════════════════════════════════════════════════

class DOMLadderView(QTableView):
    """Anchored price ladder.  Emits ``ladder_clicked(action, price)``
    where ``action == "BUY"`` for a bid-size cell click and ``"SELL"``
    for an ask-size cell click; price-column clicks are ignored."""

    ladder_clicked = Signal(str, float)

    def __init__(self, parent=None, *, model: DOMLadderModel | None = None) -> None:
        super().__init__(parent)
        self._model = model or DOMLadderModel()
        # When locked (default), every sync_to_last() call scrolls the
        # viewport so the last-price row sits in the middle.  When
        # unlocked the user's manual scroll position is preserved so
        # they can inspect deeper levels without the view yanking back
        # to the centre on every tick.
        self._locked: bool = True
        self.setModel(self._model)
        self.setItemDelegate(DOMLadderDelegate(self))

        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(DOMLadderDelegate._ROW_H)
        self.setShowGrid(False)
        self.setStyleSheet(
            f"QTableView{{background:{Theme.BG_PANEL};"
            f"border:1px solid {Theme.BORDER};"
            f"border-radius:4px;gridline-color:{Theme.BORDER}}}"
            f"QTableView::item{{padding:0px;border:none}}")

        # Stretch all three columns to fill the view width equally — the
        # view sits inside a parent layout that can be wider than the
        # columns' natural size; without Stretch the ladder would have
        # dead space to the right of the ASK column.
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # No visible scrollbar — the 41-row ladder still scrolls via
        # mouse wheel / trackpad, but the bar itself is hidden so the
        # ladder reads as a clean column.
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # When the model rebuilds (recenter / tick-size change), snap
        # the viewport so the last-price row sits in the middle — keeps
        # the interesting prices visible without manual scrolling.
        self._model.modelReset.connect(self._scroll_to_last)

        # ── Hover-side badge ─────────────────────────────────────────────────
        # Pre-click intent indicator: a small green BUY / red SELL chip
        # follows the cursor while hovering over the ladder so the
        # trader sees what a click here would submit *before* clicking.
        # Mirrors the same side rule mousePressEvent uses.
        self.viewport().setMouseTracking(True)
        self.setMouseTracking(True)
        self._hover_badge = QLabel(self.viewport())
        self._hover_badge.setObjectName("ladderHoverBadge")
        self._hover_badge.setFont(QFont("Helvetica Neue, Arial", 10, QFont.Weight.Bold))
        self._hover_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._hover_badge.hide()

    def model(self) -> DOMLadderModel:  # type: ignore[override]
        return self._model

    # ── Hover-side helpers ───────────────────────────────────────────────────

    @staticmethod
    def _side_at(pos, col: int, cell_rect) -> str | None:
        """Return ``"BUY"`` / ``"SELL"`` for *pos* over a cell at
        *col* with bounding *cell_rect*, mirroring the click logic.
        ``None`` for cells/positions that don't trade."""
        if col == COL_BID_SZ:
            return "BUY"
        if col == COL_ASK_SZ:
            return "SELL"
        if col == COL_PRICE:
            return "BUY" if pos.x() < cell_rect.center().x() else "SELL"
        return None

    def _show_hover_badge(self, side: str, anchor) -> None:
        """Render the badge at *side* (BUY / SELL) and reposition it
        next to *anchor* (a QPoint in viewport coordinates).  Offset
        slightly so the badge doesn't sit under the cursor."""
        if side == "BUY":
            bg, fg = "#1A2A1A", "#3FB950"
            text = "BUY"
        else:
            bg, fg = "#2A1818", "#F85149"
            text = "SELL"
        self._hover_badge.setText(text)
        self._hover_badge.setStyleSheet(
            f"QLabel#ladderHoverBadge {{"
            f"background:{bg};color:{fg};"
            f"border:1px solid {fg};border-radius:3px;"
            f"padding:1px 6px}}")
        self._hover_badge.adjustSize()
        x = anchor.x() + 14
        y = anchor.y() + 2
        # Keep the badge inside the viewport so it doesn't get clipped
        # at the right/bottom edges.
        vp = self.viewport().rect()
        bw = self._hover_badge.width()
        bh = self._hover_badge.height()
        if x + bw > vp.right():
            x = anchor.x() - bw - 6
        if y + bh > vp.bottom():
            y = vp.bottom() - bh
        self._hover_badge.move(x, y)
        self._hover_badge.show()
        self._hover_badge.raise_()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid():
            rect = self.visualRect(idx)
            side = self._side_at(pos, idx.column(), rect)
            if side is not None:
                self._show_hover_badge(side, pos)
            else:
                self._hover_badge.hide()
        else:
            self._hover_badge.hide()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_badge.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Route clicks to BUY / SELL limit orders:

        * bid-size cell   → BUY LMT at that row's price
        * ask-size cell   → SELL LMT at that row's price
        * price cell      → split horizontally:
                              left half  → BUY  LMT at that price
                              right half → SELL LMT at that price

        This mirrors what most pro DOM ladders do — a single click
        anywhere on the ladder submits an order at the row's price,
        and the horizontal side picks the direction.
        """
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid():
            row = idx.row()
            col = idx.column()
            prices = self._model.prices
            if 0 <= row < len(prices):
                price = float(prices[row])
                if col == COL_BID_SZ:
                    self.ladder_clicked.emit("BUY", price)
                elif col == COL_ASK_SZ:
                    self.ladder_clicked.emit("SELL", price)
                elif col == COL_PRICE:
                    rect = self.visualRect(idx)
                    if pos.x() < rect.center().x():
                        self.ladder_clicked.emit("BUY", price)
                    else:
                        self.ladder_clicked.emit("SELL", price)
        super().mousePressEvent(event)

    def _scroll_to_last(self) -> None:
        """Centre the viewport on the last-price row after a model reset.

        ``scrollTo(index, PositionAtCenter)`` puts the row in the middle
        of the visible area; for ladders shorter than the viewport this
        is a no-op (Qt pins to either end when there's nothing to
        scroll).  Falls back to the middle row index when there is no
        last price yet (cold start).
        """
        prices = self._model.prices
        if not prices:
            return
        last = self._model.last_price
        target_row = len(prices) // 2
        if last > 0:
            for i, p in enumerate(prices):
                if abs(p - last) < self._model.tick_size / 2:
                    target_row = i
                    break
        self.scrollTo(
            self._model.index(target_row, COL_PRICE),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    def set_locked(self, locked: bool) -> None:
        """Lock / unlock the 'keep last price centred' behaviour.

        When newly locked, immediately snap the viewport to the last
        price so the user's next glance matches the UI affordance.
        """
        self._locked = bool(locked)
        if self._locked:
            self._scroll_to_last()

    @property
    def is_locked(self) -> bool:
        return self._locked

    # Safety valve: when unlocked, if the last-price row drifts this
    # many rows past the centre of the viewport, we recentre anyway so
    # the user doesn't completely lose sight of the market.
    _DRIFT_LIMIT_ROWS = 20

    def sync_to_last(self) -> None:
        """Ask the view to re-centre on the last price.

        No longer yanks the viewport on every tick — the model owns a
        debounced auto-recenter that fires on drift > 10 rows after a
        3-second delay, and its ``modelReset`` snaps the viewport
        automatically.  This method is kept as a safety valve: if the
        last-price row has drifted more than ``_DRIFT_LIMIT_ROWS`` from
        the viewport centre (e.g. user scrolled way off), pull it back
        into view so the market doesn't disappear entirely.
        """
        drift = self._last_row_drift()
        if drift is not None and drift > self._DRIFT_LIMIT_ROWS:
            self._scroll_to_last()

    def _last_row_drift(self) -> int | None:
        """Return the absolute row-distance between the last-price row
        and the row currently showing in the middle of the viewport,
        or ``None`` if either can't be determined yet."""
        prices = self._model.prices
        last = self._model.last_price
        if not prices or last <= 0:
            return None
        last_row = -1
        for i, p in enumerate(prices):
            if abs(p - last) < self._model.tick_size / 2:
                last_row = i
                break
        if last_row < 0:
            # Last price has moved entirely off the ladder grid — treat
            # that as "max drift" so we snap back to show it.
            return self._DRIFT_LIMIT_ROWS + 1
        # Middle of the visible viewport in viewport coordinates.
        vp = self.viewport()
        mid_y = vp.height() // 2
        mid_idx = self.indexAt(vp.rect().topLeft() + QPoint(0, mid_y))
        if not mid_idx.isValid():
            return None
        return abs(last_row - mid_idx.row())
