"""
BookTraderView - Depth-of-Market (DOM) price ladder for order entry.

Replicates the TWS BookTrader:
  - Vertical price ladder centred on the last-traded price
  - Bid / Ask depth with proportional coloured bars
  - Bold text on round-number price levels
  - Last-traded row highlighted with "qty @ price" annotation
  - Click bid column to BUY LMT, click ask column to SELL LMT
  - Auto-centre with a manual re-centre button
  - Lightweight demo data so the view works immediately without TWS
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager

log = logging.getLogger(__name__)

# -- Theme tokens -------------------------------------------------------------
BG_MAIN     = "#161a25"
BG_PANEL    = "#1e2230"
BG_HEADER   = "#14171D"
BORDER      = "#2A2E37"
TEXT        = "#C9D1D9"
TEXT_DIM    = "#8B949E"
TEXT_BRIGHT = "#E6EDF3"
GREEN       = "#2e7d32"
RED         = "#c62828"
BLUE        = "#58A6FF"

BID_BAR      = "#4A3C10"
BID_BAR_FILL = "#6B5A1A"
ASK_BAR      = "#0E3E30"
ASK_BAR_FILL = "#186050"

LAST_ROW_BG   = "#0E3460"
LAST_ROW_EDGE = "#1A4E80"

# -- Column indices ------------------------------------------------------------
COL_BID_ORD = 0
COL_BID_SZ  = 1
COL_PRICE   = 2
COL_ASK_SZ  = 3
COL_ASK_ORD = 4

_HEADERS  = ["Bid #", "Bid Qty", "Price", "Ask Qty", "Ask #"]
_NUM_ROWS = 400


# ==============================================================================
#  Model
# ==============================================================================

class PriceLadderModel(QAbstractTableModel):
    """Row-per-tick model.  Row 0 = highest price, last row = lowest."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tick: Decimal = Decimal("0.25")
        self._prices: list[Decimal] = []

        self._bids: dict[Decimal, int] = {}
        self._asks: dict[Decimal, int] = {}
        self._max_bid: int = 1
        self._max_ask: int = 1

        self._bid_orders: dict[Decimal, int] = {}
        self._ask_orders: dict[Decimal, int] = {}

        self._last: Decimal = Decimal("0")
        self._last_sz: int = 0

        self._total_bid_sz: int = 0
        self._total_ask_sz: int = 0

    def set_tick(self, tick) -> None:
        self._tick = Decimal(str(tick))

    @property
    def tick(self) -> Decimal:
        return self._tick

    def rebuild(self, centre: float, n: int = _NUM_ROWS) -> None:
        self.beginResetModel()
        # Snap to the nearest tick *multiple* (not just decimal places)
        raw = Decimal(str(centre))
        c = (raw / self._tick).quantize(Decimal("1")) * self._tick
        half = n // 2
        high = c + self._tick * half
        self._prices = [high - self._tick * i for i in range(n)]
        self.endResetModel()

    def row_of(self, price: Decimal) -> int:
        if not self._prices or self._tick == 0:
            return -1
        top = self._prices[0]
        offset = (top - price) / self._tick
        idx = int(offset)
        if 0 <= idx < len(self._prices) and self._prices[idx] == price:
            return idx
        return -1

    def price_at(self, row: int) -> Optional[Decimal]:
        if 0 <= row < len(self._prices):
            return self._prices[row]
        return None

    # -- market-data feeds -----------------------------------------------------

    def feed_depth(self, bids: dict[float, int], asks: dict[float, int]) -> None:
        self._bids = {Decimal(str(p)): s for p, s in bids.items()}
        self._asks = {Decimal(str(p)): s for p, s in asks.items()}
        self._max_bid = max(self._bids.values(), default=1) or 1
        self._max_ask = max(self._asks.values(), default=1) or 1
        self._total_bid_sz = sum(self._bids.values())
        self._total_ask_sz = sum(self._asks.values())
        self._emit_columns(COL_BID_SZ, COL_ASK_SZ)
        self.headerDataChanged.emit(
            Qt.Orientation.Horizontal, COL_BID_SZ, COL_ASK_SZ)

    def feed_last(self, price: float, size: int = 0) -> None:
        old = self._last
        self._last = Decimal(str(price))
        self._last_sz = size
        for p in (old, self._last):
            r = self.row_of(p)
            if r >= 0:
                self._emit_row(r)

    def feed_orders(self, bids: dict[float, int], asks: dict[float, int]) -> None:
        self._bid_orders = {Decimal(str(p)): q for p, q in bids.items()}
        self._ask_orders = {Decimal(str(p)): q for p, q in asks.items()}
        self._emit_columns(COL_BID_ORD, COL_ASK_ORD)

    # -- Qt model interface ----------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._prices)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 5

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section == COL_BID_SZ and self._total_bid_sz:
                return f"Bid Qty ({self._total_bid_sz})"
            if section == COL_ASK_SZ and self._total_ask_sz:
                return f"Ask Qty ({self._total_ask_sz})"
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row >= len(self._prices):
            return None
        price = self._prices[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_PRICE:
                return str(price)
            if col == COL_BID_SZ:
                v = self._bids.get(price, 0)
                return str(v) if v else ""
            if col == COL_ASK_SZ:
                v = self._asks.get(price, 0)
                return str(v) if v else ""
            if col == COL_BID_ORD:
                v = self._bid_orders.get(price, 0)
                return str(v) if v else ""
            if col == COL_ASK_ORD:
                v = self._ask_orders.get(price, 0)
                return str(v) if v else ""
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_PRICE:
                return int(Qt.AlignmentFlag.AlignCenter)
            if col in (COL_BID_SZ, COL_BID_ORD):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.UserRole:
            bid_sz = self._bids.get(price, 0)
            ask_sz = self._asks.get(price, 0)
            return {
                "price": price,
                "bid": bid_sz,
                "ask": ask_sz,
                "max_bid": self._max_bid,
                "max_ask": self._max_ask,
                "is_last": price == self._last,
                "last_sz": self._last_sz if price == self._last else 0,
                "bid_ord": self._bid_orders.get(price, 0),
                "ask_ord": self._ask_orders.get(price, 0),
            }

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )

    def _emit_columns(self, c1: int, c2: int) -> None:
        if self._prices:
            self.dataChanged.emit(
                self.index(0, c1),
                self.index(len(self._prices) - 1, c2),
            )

    def _emit_row(self, row: int) -> None:
        self.dataChanged.emit(
            self.index(row, 0),
            self.index(row, self.columnCount() - 1),
        )


# ==============================================================================
#  Delegate
# ==============================================================================

class PriceLadderDelegate(QStyledItemDelegate):
    """Custom-paints each cell: depth bars, price text, highlights."""

    ROW_H = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font = QFont("Menlo", 11)
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        self._font_bold = QFont("Menlo", 11)
        self._font_bold.setStyleHint(QFont.StyleHint.Monospace)
        self._font_bold.setWeight(QFont.Weight.Bold)
        self._font_sm = QFont("Menlo", 9)
        self._font_sm.setStyleHint(QFont.StyleHint.Monospace)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect
        col = index.column()
        info: dict = index.data(Qt.ItemDataRole.UserRole) or {}
        text: str = index.data(Qt.ItemDataRole.DisplayRole) or ""
        price: Decimal = info.get("price", Decimal(0))
        bid: int = info.get("bid", 0)
        ask: int = info.get("ask", 0)
        max_bid: int = info.get("max_bid", 1)
        max_ask: int = info.get("max_ask", 1)
        is_last: bool = info.get("is_last", False)
        last_sz: int = info.get("last_sz", 0)
        bid_ord: int = info.get("bid_ord", 0)
        ask_ord: int = info.get("ask_ord", 0)

        # 1. base background
        painter.fillRect(rect, QColor(BG_MAIN))

        # 2. bid / ask depth bars
        if col == COL_BID_SZ and bid > 0:
            frac = min(bid / max_bid, 1.0)
            bar_w = max(int(rect.width() * frac), 4)
            bar_rect = rect.adjusted(rect.width() - bar_w, 1, 0, -1)
            painter.fillRect(bar_rect, QColor(BID_BAR))
            if frac > 0.5:
                inner = bar_rect.adjusted(bar_rect.width() // 3, 0, 0, 0)
                c = QColor(BID_BAR_FILL)
                c.setAlpha(120)
                painter.fillRect(inner, c)
        elif col == COL_ASK_SZ and ask > 0:
            frac = min(ask / max_ask, 1.0)
            bar_w = max(int(rect.width() * frac), 4)
            bar_rect = rect.adjusted(0, 1, -(rect.width() - bar_w), -1)
            painter.fillRect(bar_rect, QColor(ASK_BAR))
            if frac > 0.5:
                inner = bar_rect.adjusted(0, 0, -(bar_rect.width() // 3), 0)
                c = QColor(ASK_BAR_FILL)
                c.setAlpha(120)
                painter.fillRect(inner, c)

        # 3. price column colouring
        if col == COL_PRICE:
            if is_last:
                painter.fillRect(rect, QColor(LAST_ROW_BG))
                painter.setPen(QPen(QColor(LAST_ROW_EDGE), 1))
                painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
                painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
            elif bid > 0:
                c = QColor(BID_BAR); c.setAlpha(80)
                painter.fillRect(rect, c)
            elif ask > 0:
                c = QColor(ASK_BAR); c.setAlpha(80)
                painter.fillRect(rect, c)

        # 4. subtle row tint for last-traded on non-price cols
        if is_last and col != COL_PRICE:
            c = QColor(LAST_ROW_BG); c.setAlpha(40)
            painter.fillRect(rect, c)

        # 5. working order highlights
        if col == COL_BID_ORD and bid_ord > 0:
            c = QColor(GREEN); c.setAlpha(50)
            painter.fillRect(rect, c)
        elif col == COL_ASK_ORD and ask_ord > 0:
            c = QColor(RED); c.setAlpha(50)
            painter.fillRect(rect, c)

        # 6. grid lines
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomRight())

        # 7. text
        if not text:
            painter.restore()
            return

        is_round = False
        if col == COL_PRICE:
            try:
                if price == int(price):
                    is_round = True
                elif price % 1 == Decimal("0.50") or price % 1 == Decimal("0.5"):
                    is_round = True
            except Exception:
                pass

        if col in (COL_BID_ORD, COL_ASK_ORD):
            painter.setFont(self._font_sm)
        elif is_round and col == COL_PRICE:
            painter.setFont(self._font_bold)
        else:
            painter.setFont(self._font)

        if col == COL_BID_SZ:
            painter.setPen(QColor("#D4A82A") if bid > 0 else QColor(TEXT_DIM))
        elif col == COL_ASK_SZ:
            painter.setPen(QColor("#4AD080") if ask > 0 else QColor(TEXT_DIM))
        elif col == COL_PRICE:
            if is_last:
                painter.setPen(QColor("#FFFFFF"))
            elif is_round:
                painter.setPen(QColor(TEXT_BRIGHT))
            else:
                painter.setPen(QColor(TEXT))
        elif col == COL_BID_ORD:
            painter.setPen(QColor("#66BB6A") if bid_ord > 0 else QColor(TEXT_DIM))
        elif col == COL_ASK_ORD:
            painter.setPen(QColor("#EF5350") if ask_ord > 0 else QColor(TEXT_DIM))
        else:
            painter.setPen(QColor(TEXT_DIM))

        align = index.data(Qt.ItemDataRole.TextAlignmentRole)
        if align is None:
            align = int(Qt.AlignmentFlag.AlignCenter)
        else:
            align = int(align)

        draw_text = text
        if col == COL_PRICE and is_last and last_sz:
            draw_text = f"{last_sz} @ {text}"

        painter.drawText(rect.adjusted(6, 0, -6, 0), align, draw_text)
        painter.restore()

    def sizeHint(self, option, index: QModelIndex):
        sh = super().sizeHint(option, index)
        sh.setHeight(self.ROW_H)
        col = index.column()
        if col == COL_PRICE:
            sh.setWidth(120)
        elif col in (COL_BID_SZ, COL_ASK_SZ):
            sh.setWidth(90)
        else:
            sh.setWidth(50)
        return sh


# ==============================================================================
#  Stylesheet fragments
# ==============================================================================

_TABLE_QSS = f"""
QTableView {{
    background-color: {BG_MAIN};
    color: {TEXT};
    gridline-color: {BORDER};
    border: none;
    outline: none;
    selection-background-color: #1A3050;
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background-color: {BG_HEADER};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 4px 6px;
    font-weight: 600;
    font-size: 10px;
    font-family: "Menlo", monospace;
}}
QScrollBar:vertical {{
    background: {BG_MAIN};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #3A3E48;
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; width: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}
"""


def _make_btn(text, bg, fg, hover, min_w=60):
    btn = QPushButton(text)
    btn.setFixedHeight(26)
    btn.setMinimumWidth(min_w)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton{{background:{bg};color:{fg};"
        f"border:1px solid {bg};border-radius:3px;"
        f"padding:2px 8px;font-size:11px;font-weight:600;}}"
        f"QPushButton:hover{{background:{hover};}}"
        f"QPushButton:pressed{{background:{bg};}}"
    )
    return btn


def _toolbar_label(text, color=TEXT_DIM, size=10, bold=False):
    weight = "600" if bold else "normal"
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"QLabel{{color:{color};background:transparent;"
        f"font-size:{size}px;font-weight:{weight};"
        f"font-family:\'Menlo\',monospace;}}"
    )
    return lbl


_INPUT_QSS = (
    f"QLineEdit{{background:{BG_MAIN};color:{TEXT};"
    f"border:1px solid {BORDER};border-radius:3px;"
    f"padding:3px 6px;font-size:12px;font-family:\'Menlo\',monospace;}}"
    f"QLineEdit:focus{{border-color:{BLUE};}}"
)
_SPIN_QSS = (
    f"QSpinBox{{background:{BG_MAIN};color:{TEXT};"
    f"border:1px solid {BORDER};border-radius:3px;"
    f"padding:3px 4px;font-size:12px;min-width:50px;"
    f"font-family:\'Menlo\',monospace;}}"
    f"QSpinBox:focus{{border-color:{BLUE};}}"
    f"QSpinBox::up-button, QSpinBox::down-button{{"
    f"background:{BG_PANEL};border:none;width:14px;}}"
)
_COMBO_QSS = (
    f"QComboBox{{background:{BG_MAIN};color:{TEXT};"
    f"border:1px solid {BORDER};border-radius:3px;"
    f"padding:3px 6px;font-size:11px;min-width:55px;"
    f"font-family:\'Menlo\',monospace;}}"
    f"QComboBox::drop-down{{border:none;width:16px;}}"
    f"QComboBox QAbstractItemView{{background:{BG_MAIN};color:{TEXT};"
    f"selection-background-color:#253545;}}"
)


# ==============================================================================
#  Main widget
# ==============================================================================

class BookTraderView(QWidget):
    """
    Full Book Trader / DOM price-ladder widget.
    Emits order_requested(dict) when the user clicks to place an order.
    """

    order_requested = Signal(object)

    # Default tick sizes by symbol (common futures)
    _TICK_MAP = {
        "ES": "0.25", "MES": "0.25", "NQ": "0.25", "MNQ": "0.25",
        "RTY": "0.10", "M2K": "0.10", "YM": "1.00", "MYM": "1.00",
        "CL": "0.01", "MCL": "0.01", "GC": "0.10", "MGC": "0.10",
    }

    def __init__(self, active_threads=None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        self._model = PriceLadderModel(self)
        self._symbol: str = ""
        self._auto_centre: bool = True

        # External state – keep the *same* dict reference so workers added later are visible
        self._active_threads = active_threads if active_threads is not None else {}
        self._current_worker = None          # IBWorkerThread feeding us depth
        self._current_contract: dict = {}    # contract dict for current sub
        self._saved_contracts: list[dict] = []

        self._build_ui()
        self._reload_contracts()
        self._load_demo_data()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- top toolbar -------------------------------------------------------
        toolbar = QWidget()
        toolbar.setFixedHeight(38)
        toolbar.setStyleSheet(f"background:{BG_PANEL};")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 4, 8, 4)
        tb.setSpacing(6)

        tb.addWidget(_toolbar_label("Sym", TEXT_DIM, 11))
        self._sym_input = QLineEdit()
        self._sym_input.setPlaceholderText("Search symbol...")
        self._sym_input.setFixedWidth(120)
        self._sym_input.setStyleSheet(_INPUT_QSS)
        self._sym_input.returnPressed.connect(self._on_sym_enter)

        # Auto-complete from saved contracts
        self._completer = QCompleter([], self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(12)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.activated.connect(self._on_completer_activated)
        self._sym_input.setCompleter(self._completer)

        tb.addWidget(self._sym_input)

        tb.addWidget(_toolbar_label("Qty", TEXT_DIM, 11))
        self._qty_spin = QSpinBox()
        self._qty_spin.setRange(1, 9999)
        self._qty_spin.setValue(1)
        self._qty_spin.setStyleSheet(_SPIN_QSS)
        tb.addWidget(self._qty_spin)

        tb.addWidget(_toolbar_label("Tick", TEXT_DIM, 11))
        self._tick_combo = QComboBox()
        self._tick_combo.addItems(["0.25", "0.50", "0.01", "0.05", "0.10", "1.00"])
        self._tick_combo.setStyleSheet(_COMBO_QSS)
        self._tick_combo.currentTextChanged.connect(self._on_tick_changed)
        tb.addWidget(self._tick_combo)

        tb.addSpacing(12)

        self._buy_mkt_btn = _make_btn("BUY MKT", GREEN, "#FFFFFF", "#388E3C")
        self._buy_mkt_btn.clicked.connect(self._on_buy_mkt)
        tb.addWidget(self._buy_mkt_btn)

        self._sell_mkt_btn = _make_btn("SELL MKT", RED, "#FFFFFF", "#D32F2F")
        self._sell_mkt_btn.clicked.connect(self._on_sell_mkt)
        tb.addWidget(self._sell_mkt_btn)

        tb.addStretch()

        self._centre_btn = _make_btn("Centre", "#1A2440", BLUE, "#24304A", min_w=72)
        self._centre_btn.setStyleSheet(
            self._centre_btn.styleSheet().replace(
                "border:1px solid #1A2440", "border:1px solid #2A3450"))
        self._centre_btn.clicked.connect(self._scroll_to_last)
        tb.addWidget(self._centre_btn)

        self._info_lbl = _toolbar_label("No contract", TEXT_DIM, 11)
        tb.addWidget(self._info_lbl)

        root.addWidget(toolbar)

        # -- separator ---------------------------------------------------------
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{BORDER};")
        root.addWidget(sep)

        # -- price ladder table ------------------------------------------------
        self._table = QTableView()
        self._table.setModel(self._model)
        self._delegate = PriceLadderDelegate(self._table)
        self._table.setItemDelegate(self._delegate)
        self._table.setStyleSheet(_TABLE_QSS)
        self._table.setShowGrid(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(PriceLadderDelegate.ROW_H)

        hh = self._table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(40)
        hh.setSectionResizeMode(COL_BID_ORD, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_BID_SZ, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_PRICE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_ASK_SZ, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_ASK_ORD, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(COL_BID_ORD, 50)
        hh.resizeSection(COL_PRICE, 120)
        hh.resizeSection(COL_ASK_ORD, 50)

        self._table.clicked.connect(self._on_cell_click)
        root.addWidget(self._table, stretch=1)

        # -- bottom info bar ---------------------------------------------------
        bottom = QWidget()
        bottom.setFixedHeight(28)
        bottom.setStyleSheet(f"background:{BG_PANEL};")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(8, 2, 8, 2)
        bl.setSpacing(16)

        self._lbl_last   = _toolbar_label("Last: ---", TEXT, 10, bold=True)
        self._lbl_bid    = _toolbar_label("Bid: ---", "#D4A82A", 10)
        self._lbl_ask    = _toolbar_label("Ask: ---", "#4AD080", 10)
        self._lbl_spread = _toolbar_label("Spread: ---", TEXT_DIM, 10)
        self._lbl_pos    = _toolbar_label("Pos: flat", TEXT_DIM, 10)

        bl.addWidget(self._lbl_last)
        bl.addWidget(self._lbl_bid)
        bl.addWidget(self._lbl_ask)
        bl.addWidget(self._lbl_spread)
        bl.addStretch()
        bl.addWidget(self._lbl_pos)

        root.addWidget(bottom)

    # -- demo data -------------------------------------------------------------

    def _load_demo_data(self) -> None:
        centre = 21250.00
        self._symbol = "NQ"
        self._sym_input.setText("NQ")
        self._model.set_tick(0.25)
        self._model.rebuild(centre, _NUM_ROWS)

        bids = {
            21249.75: 12, 21249.50: 8,  21249.25: 15, 21249.00: 22,
            21248.75: 6,  21248.50: 9,  21248.25: 14, 21248.00: 30,
            21247.75: 11, 21247.50: 18, 21247.25: 7,  21247.00: 25,
            21246.75: 5,  21246.50: 10, 21246.25: 8,  21246.00: 35,
        }
        asks = {
            21250.00: 10, 21250.25: 14, 21250.50: 6,  21250.75: 9,
            21251.00: 20, 21251.25: 4,  21251.50: 7,  21251.75: 11,
            21252.00: 28, 21252.25: 5,  21252.50: 8,  21252.75: 3,
            21253.00: 18, 21253.25: 6,  21253.50: 10, 21254.00: 32,
        }
        self._model.feed_depth(bids, asks)
        self._model.feed_last(21250.00, size=3)
        self._model.feed_orders(
            bids={21248.00: 2, 21247.00: 5},
            asks={21252.00: 1, 21253.00: 3},
        )
        self._update_info_bar(21250.00, 21249.75, 12, 21250.00, 10)
        QTimer.singleShot(120, self._scroll_to_last)

    # -- saved contracts --------------------------------------------------------

    def _reload_contracts(self) -> None:
        """Reload saved contracts from config and rebuild the completer list."""
        self._saved_contracts = config_manager.load_contracts()
        # Build completion strings: "SYMBOL (SEC_TYPE) localSymbol"
        completions = []
        for c in self._saved_contracts:
            local = c.get("local_symbol", "")
            sym = c.get("symbol", "")
            sec = c.get("sec_type", "")
            label = f"{sym} ({sec})" if not local or local == sym else f"{local} ({sec}) {sym}"
            completions.append(label)
        from PySide6.QtCore import QStringListModel
        model = QStringListModel(completions, self._completer)
        self._completer.setModel(model)
        log.info("BookTrader: loaded %d saved contracts for completer", len(completions))

    def _find_contract_by_text(self, text: str) -> dict | None:
        """Match user text against saved contracts.

        Tries: localSymbol exact, then symbol exact, then the completer label.
        """
        text_up = text.strip().upper()
        if not text_up:
            return None
        # 1. exact localSymbol
        for c in self._saved_contracts:
            if c.get("local_symbol", "").upper() == text_up:
                return c
        # 2. exact symbol
        for c in self._saved_contracts:
            if c.get("symbol", "").upper() == text_up:
                return c
        # 3. completer label match (from activated signal)
        for c in self._saved_contracts:
            local = c.get("local_symbol", "")
            sym = c.get("symbol", "")
            sec = c.get("sec_type", "")
            label = f"{sym} ({sec})" if not local or local == sym else f"{local} ({sec}) {sym}"
            if label.upper() == text_up:
                return c
        return None

    def _find_connected_worker(self):
        """Return the first connected IBWorkerThread or None."""
        for label, worker in self._active_threads.items():
            if worker.is_ib_connected:
                return worker
        return None

    # -- live data wiring ------------------------------------------------------

    def _subscribe_live(self, contract: dict) -> None:
        """Subscribe to market depth for *contract* using a connected worker."""
        # Disconnect old worker signals
        self._unsubscribe_live()

        worker = self._find_connected_worker()
        if worker is None:
            self._info_lbl.setText("No TWS connected")
            log.warning("BookTrader: no connected worker for depth subscription")
            return

        self._current_worker = worker
        self._current_contract = contract

        # Connect signals
        worker.depth_updated.connect(self._on_depth_data)
        worker.ticker_updated.connect(self._on_ticker_data)

        # Request subscription
        worker.request_mkt_depth(contract)

        sym = contract.get("local_symbol") or contract.get("symbol", "?")
        self._info_lbl.setText(f"Subscribing {sym}...")
        log.info("BookTrader: subscribing depth via worker %s for %s",
                 worker._label, sym)

    def _unsubscribe_live(self) -> None:
        """Cancel the current depth subscription and disconnect signals."""
        if self._current_worker is not None:
            try:
                self._current_worker.depth_updated.disconnect(self._on_depth_data)
                self._current_worker.ticker_updated.disconnect(self._on_ticker_data)
            except (RuntimeError, TypeError):
                pass
            self._current_worker.cancel_mkt_depth()
            self._current_worker = None
        self._current_contract = {}

    def _on_depth_data(self, bids, asks) -> None:
        """Slot: receive live depth from the worker."""
        bb = max(bids, default=0)          # best bid – always on a tick boundary
        ba = min(asks, default=0)          # best ask – always on a tick boundary

        # Use best-bid as the rebuild anchor (it's always tick-aligned)
        anchor = float(bb or ba or 0)

        if anchor > 0 and self._model._prices:
            top = float(self._model._prices[0])
            bot = float(self._model._prices[-1])
            if anchor > top or anchor < bot:
                self._model.rebuild(anchor, _NUM_ROWS)
                log.info("BookTrader: rebuilt ladder around %.2f", anchor)
        elif anchor > 0 and not self._model._prices:
            self._model.rebuild(anchor, _NUM_ROWS)

        self._model.feed_depth(bids, asks)

        # If no ticker data has arrived yet, seed _last from the best bid
        # so that scroll-to-last has a valid target
        if anchor > 0 and self._model._last == Decimal("0"):
            self._model._last = Decimal(str(anchor))

        self._update_info_bar(
            float(self._model._last),
            bb, bids.get(bb, 0),
            ba, asks.get(ba, 0))

        if self._auto_centre and anchor > 0:
            self._scroll_to_last()

    def _on_ticker_data(self, price: float, size: int) -> None:
        """Slot: receive live last-price from the worker."""
        if price <= 0:
            return
        old_last = float(self._model._last)
        self._model.feed_last(price, size)
        self._lbl_last.setText(f"Last: {price:,.2f}")

        # Rebuild the ladder if the price drifted outside the current range
        if self._model._prices:
            top = float(self._model._prices[0])
            bot = float(self._model._prices[-1])
            if price > top or price < bot:
                self._model.rebuild(price, _NUM_ROWS)

        if self._auto_centre:
            self._scroll_to_last()

    # -- toolbar slots ---------------------------------------------------------

    def _on_sym_enter(self) -> None:
        text = self._sym_input.text().strip()
        if not text:
            return

        contract = self._find_contract_by_text(text)
        if contract is None:
            self._info_lbl.setText(f"'{text}' not in saved contracts")
            log.info("BookTrader: no saved contract for '%s'", text)
            return

        self._apply_contract(contract)

    def _on_completer_activated(self, text: str) -> None:
        """User picked an item from the completer dropdown."""
        contract = self._find_contract_by_text(text)
        if contract:
            self._apply_contract(contract)

    def _apply_contract(self, contract: dict) -> None:
        """Set up the ladder for *contract* and subscribe to live data."""
        sym = contract.get("symbol", "")
        local = contract.get("local_symbol", "") or sym

        # Guard: skip if we're already subscribed to the same symbol
        if local == self._symbol and self._current_worker is not None:
            return

        self._symbol = local
        self._sym_input.setText(local)

        # Auto-select tick size
        tick = self._TICK_MAP.get(sym.upper())
        if tick is None:
            sec = contract.get("sec_type", "")
            tick = "0.01" if sec == "STK" else "0.25"
        idx = self._tick_combo.findText(tick)
        if idx >= 0:
            self._tick_combo.setCurrentIndex(idx)
        else:
            self._model.set_tick(tick)

        # Clear stale data and reset the last-traded price
        self._model._last = Decimal("0")
        self._model._last_sz = 0
        self._model.feed_depth({}, {})
        self._model.feed_orders({}, {})
        self._model.rebuild(0, _NUM_ROWS)

        self._info_lbl.setText(f"Connecting {local}...")

        # Subscribe to live depth
        self._subscribe_live(contract)

    def _on_tick_changed(self, text: str) -> None:
        try:
            self._model.set_tick(text)
            if self._model._prices:
                c = float(self._model._last) or float(
                    self._model._prices[len(self._model._prices) // 2])
                self._model.rebuild(c, _NUM_ROWS)
                QTimer.singleShot(50, self._scroll_to_last)
        except Exception:
            pass

    def _on_buy_mkt(self) -> None:
        if not self._symbol:
            return
        qty = self._qty_spin.value()
        self.order_requested.emit({
            "action": "BUY", "quantity": qty,
            "price": 0.0, "order_type": "MKT",
            "symbol": self._symbol,
        })
        log.info("BookTrader BUY MKT %d %s", qty, self._symbol)

    def _on_sell_mkt(self) -> None:
        if not self._symbol:
            return
        qty = self._qty_spin.value()
        self.order_requested.emit({
            "action": "SELL", "quantity": qty,
            "price": 0.0, "order_type": "MKT",
            "symbol": self._symbol,
        })
        log.info("BookTrader SELL MKT %d %s", qty, self._symbol)

    # -- ladder click -> limit order -------------------------------------------

    def _on_cell_click(self, index: QModelIndex) -> None:
        col = index.column()
        price = self._model.price_at(index.row())
        if price is None:
            return
        qty = self._qty_spin.value()
        if col in (COL_BID_SZ, COL_BID_ORD):
            self.order_requested.emit({
                "action": "BUY", "quantity": qty,
                "price": float(price), "order_type": "LMT",
                "symbol": self._symbol,
            })
            log.info("BookTrader BUY LMT %d @ %s", qty, price)
        elif col in (COL_ASK_SZ, COL_ASK_ORD):
            self.order_requested.emit({
                "action": "SELL", "quantity": qty,
                "price": float(price), "order_type": "LMT",
                "symbol": self._symbol,
            })
            log.info("BookTrader SELL LMT %d @ %s", qty, price)

    def _scroll_to_last(self) -> None:
        row = self._model.row_of(self._model._last)
        if row >= 0:
            idx = self._model.index(row, COL_PRICE)
            self._table.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)

    # -- public live-data API --------------------------------------------------

    def feed_depth(self, bids, asks) -> None:
        self._model.feed_depth(bids, asks)
        bb = max(bids, default=0)
        ba = min(asks, default=0)
        self._update_info_bar(
            float(self._model._last), bb, bids.get(bb, 0), ba, asks.get(ba, 0))

    def feed_last(self, price: float, size: int = 0) -> None:
        self._model.feed_last(price, size)
        self._lbl_last.setText(f"Last: {price:,.2f}")
        if self._auto_centre:
            self._scroll_to_last()

    def feed_orders(self, bid_ords, ask_ords) -> None:
        self._model.feed_orders(bid_ords, ask_ords)

    # -- helpers ---------------------------------------------------------------

    def _update_info_bar(self, last, bid, bid_sz, ask, ask_sz) -> None:
        spread = ask - bid if ask and bid else 0
        self._lbl_last.setText(f"Last: {last:,.2f}")
        self._lbl_bid.setText(f"Bid: {bid:,.2f} [{bid_sz}]")
        self._lbl_ask.setText(f"Ask: {ask:,.2f} [{ask_sz}]")
        self._lbl_spread.setText(f"Spread: {spread:.2f}")
        self._info_lbl.setText(
            f"{self._symbol}  {last:,.2f}" if self._symbol else "No contract")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG_MAIN))
        p.end()
