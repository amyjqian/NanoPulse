"""Caption casing on the trade tables.

The shared table chrome draws captions in an uppercase tracked-out strip.
The trade, position and order tables opt out: their captions read as
written, so "R.P&L" and "Avg buy" stay that way instead of being shouted.
Anything else on the shared chrome keeps the uppercase strip.
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QFontMetrics

from src.ui.widgets import table_theme as tt
from src.ui.widgets.trades_model import COL_REAL_PNL, HEADERS
from src.ui.widgets.trades_panel import TradesPanel
from src.ui.widgets.trades_summary_model import (
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_TRANS_TOTAL, S_HEADERS,
)


def _capitalization(table) -> QFont.Capitalization:
    return table.horizontalHeader().font().capitalization()


def test_flat_realized_pnl_caption(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    assert HEADERS[COL_REAL_PNL] == "R.P&L"
    assert panel._flat_model.column_headers()[COL_REAL_PNL] == "R.P&L"


def test_summary_captions(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    headers = panel._summary_model.column_headers()
    assert headers[S_COL_TRANS_TOTAL] == "Volume"
    assert headers[S_COL_AVG_BUY] == "Avg buy"
    assert headers[S_COL_AVG_SELL] == "Avg sell"
    # The two averages must stay distinguishable from each other.
    assert headers[S_COL_AVG_BUY] != headers[S_COL_AVG_SELL]
    assert len(set(headers)) == len(headers), "duplicate summary caption"


def test_both_trade_tables_render_captions_as_written(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    for table in (panel._flat_table, panel._summary_table):
        assert _capitalization(table) == QFont.Capitalization.MixedCase
    # Sentence case: a leading capital, nothing shouted after it.  "P&L"
    # and the like are spelled out in the header lists and stay as written.
    for caption in HEADERS + S_HEADERS:
        assert caption[:1].isupper(), caption
        assert caption != caption.upper() or not caption.isalpha(), caption


def test_the_shared_chrome_still_shouts_by_default(qtbot):
    """The opt-out is per-table, not a global restyle."""
    from PySide6.QtWidgets import QTableView
    view = QTableView()
    qtbot.addWidget(view)
    tt.apply_table_chrome(view)
    assert _capitalization(view) == QFont.Capitalization.AllUppercase


# ── Positions ───────────────────────────────────────────────────────────────

def _positions_panel(qtbot):
    from src.ui.widgets.account_position_panel import AccountPositionPanel
    panel = AccountPositionPanel()
    qtbot.addWidget(panel)
    return panel


def test_positions_captions(qtbot):
    from src.ui.widgets.account_position_model import (
        COL_DAY_PNL, COL_REALIZED_PNL, COL_UNREALIZED_PNL, HEADERS as P_HEADERS,
    )
    panel = _positions_panel(qtbot)
    assert P_HEADERS[COL_DAY_PNL] == "Daily P&L"
    assert P_HEADERS[COL_UNREALIZED_PNL] == "U.P&L"
    assert P_HEADERS[COL_REALIZED_PNL] == "R.P&L"
    assert _capitalization(panel._table) == QFont.Capitalization.MixedCase
    for caption in P_HEADERS:
        assert caption[:1].isupper(), caption
        # Sentence case: no interior capitalised words ("Avg price", not
        # "Avg Price").  Acronym-ish tokens like "P&L" carry punctuation.
        for word in caption.split()[1:]:
            assert word.islower() or not word.isalpha(), caption


def test_positions_columns_page_matches_the_table(qtbot):
    """Two header lists exist — the model's (what the table draws) and the
    panel's (what the Columns page lists).  They must not drift."""
    from src.ui.widgets.account_position_model import HEADERS as P_HEADERS
    panel = _positions_panel(qtbot)
    _keys, headers, _visible = panel.column_state("positions")
    assert headers == P_HEADERS[:len(headers)]


def test_positions_summary_captions(qtbot):
    from src.ui.widgets.positions_summary_model import (
        S_COL_POSITION, S_COL_REALIZED_PNL, S_COL_UNREALIZED_PNL,
        S_HEADERS as PS_HEADERS,
    )
    panel = _positions_panel(qtbot)
    assert PS_HEADERS[S_COL_POSITION] == "Qty"
    assert PS_HEADERS[S_COL_UNREALIZED_PNL] == "U.P&L"
    assert PS_HEADERS[S_COL_REALIZED_PNL] == "R.P&L"
    assert _capitalization(panel._summary_table) == \
        QFont.Capitalization.MixedCase
    for caption in PS_HEADERS:
        assert caption[:1].isupper(), caption
        for word in caption.split()[1:]:
            assert word.islower() or not word.isalpha(), caption
    assert len(set(PS_HEADERS)) == len(PS_HEADERS), "duplicate caption"


def test_caption_width_follows_the_header_font(qtbot):
    """default_column_width measures the caption through *hdr_fm*, so a
    mixed-case strip is sized for what it draws.  Measuring an uppercased
    copy (as it used to) would over-size every flat column."""
    cell_fm = QFontMetrics(tt.CELL_FONT)
    upper_fm = QFontMetrics(tt.COL_HEADER_FONT)
    mixed_fm = QFontMetrics(tt.COL_HEADER_FONT_MIXED)
    caption = "Strike / right"
    assert tt.default_column_width(cell_fm, mixed_fm, "", caption) < \
        tt.default_column_width(cell_fm, upper_fm, "", caption)
    # The uppercase strip is unchanged by the switch to measuring the
    # caption as passed — QFontMetrics applies the font's capitalization.
    assert tt.default_column_width(cell_fm, upper_fm, "", caption) == \
        tt.default_column_width(cell_fm, upper_fm, "", caption.upper())


# ── Orders ──────────────────────────────────────────────────────────────────

def test_order_blotter_captions(qtbot):
    """The Orders blotter reads as written too — sentence case, with TIF
    left as the acronym it is (same licence "R.P&L" gets)."""
    from src.ui.widgets.order_status_model import COL_TIF, HEADERS as O_HEADERS
    from src.ui.widgets.order_status_panel import OrderStatusPanel

    panel = OrderStatusPanel()
    qtbot.addWidget(panel)
    assert _capitalization(panel._table) == QFont.Capitalization.MixedCase
    assert O_HEADERS[COL_TIF] == "TIF"
    for caption in O_HEADERS:
        if not caption:
            continue    # the Cancel column is captionless by design
        assert caption[:1].isupper(), caption
        for word in caption.split()[1:]:
            assert word.islower() or not word.isalpha(), caption
