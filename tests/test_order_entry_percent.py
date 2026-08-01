"""OrderEntryPanel SH/% unit toggle (pytest-qt).

Covers: toggle visibility per asset class, percent payload stamping
(``qty_pct`` present only on Buy/Sell in % mode), the unresolved-size
block, per-unit state after the chip-value property fix, and the
resolved-share action labels.
"""
from __future__ import annotations

import json

import pytest

import src.ui.widgets.order_entry_panel as oe
from src.core import config_manager
from src.ui.widgets.size_unit_parts import UNIT_PCT, UNIT_SH


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({}))
    monkeypatch.setattr(
        oe, "_sec_type_for",
        lambda s: "STK" if (s or "").upper() in ("NVDA", "AAPL") else "FUT")
    p = oe.OrderEntryPanel(active_threads={})
    qtbot.addWidget(p)
    p._active_symbol = "NVDA"
    p._rebuild_qty_buttons()
    p._price_cache["NVDA"] = (182.40, 182.30, 182.50)
    p._last_price = 182.40
    p._last_bid = 182.30
    p._last_ask = 182.50
    # (account, net_liq, buying_power, held shares of the symbol)
    p.set_percent_context(lambda _sym: [("ACC1", 1_420_000.0, 0.0, 600.0)])
    return p


def test_toggle_only_for_stocks(panel):
    assert panel._btn_qty_unit is not None          # NVDA → STK
    panel._active_symbol = "MNQ"                    # FUT
    panel._rebuild_qty_buttons()
    assert panel._btn_qty_unit is None
    assert [b.property("qty_value") for b in panel._qty_btn_group] == \
        [1, 2, 3, 5]


def test_percent_mode_resolves_and_stamps_payload(panel, qtbot):
    payloads = []
    panel.order_requested.connect(payloads.append)

    # Share mode: no qty_pct in the payload.
    panel._emit_order("BUY", "MKT")
    assert payloads[-1]["qty"] == 100
    assert "qty_pct" not in payloads[-1]

    panel._btn_qty_unit.click()                     # → %
    assert panel._qty_unit == UNIT_PCT
    assert panel._active_pct == 25
    assert panel._active_qty == 1946                # 25% of $1.42M @ 182.40

    panel._emit_order("BUY", "MKT")
    assert payloads[-1]["qty"] == 1946              # NLV basis (buy)
    assert payloads[-1]["qty_pct"] == 25

    # SELL resolves from the POSITION: 25% of 600 held → 150.
    panel._emit_order("SELL", "MKT")
    assert payloads[-1]["qty"] == 150
    assert payloads[-1]["qty_pct"] == 25

    # BID/ASK LMT path stamps too.
    panel._emit_bid_ask_lmt_order(oe.OrderAction.BUY)
    assert payloads[-1]["qty_pct"] == 25

    # Back to shares: original qty semantics restored.
    panel._btn_qty_unit.click()
    panel._emit_order("SELL", "MKT")
    assert payloads[-1]["qty"] == 100
    assert "qty_pct" not in payloads[-1]


def test_unresolved_percent_blocks_order(panel):
    panel.set_percent_context(lambda _sym: [])      # no accounts checked
    panel._btn_qty_unit.click()
    assert panel._active_qty == 0
    payloads = []
    panel.order_requested.connect(payloads.append)
    panel._emit_order("BUY", "MKT")
    assert payloads == []                           # blocked, not 0-qty


def test_percent_sell_blocked_when_flat(panel):
    # Account has NLV but holds no shares → BUY works, SELL blocks.
    panel.set_percent_context(
        lambda _sym: [("ACC1", 1_420_000.0, 0.0, 0.0)])
    panel._btn_qty_unit.click()
    payloads = []
    panel.order_requested.connect(payloads.append)
    panel._emit_order("SELL", "MKT")
    assert payloads == []                           # nothing to sell
    panel._emit_order("BUY", "MKT")
    assert payloads[-1]["qty"] == 1946


def test_action_labels_show_resolved_shares(panel):
    panel._btn_qty_unit.click()
    panel._refresh_action_labels()
    # The §5 safety readout — resolved count visible before any click,
    # per side: Buy from net liq, Sell from the held position.
    assert panel._btn_buy_mkt._primary == "Buy 25% ≈1,946 @MKT"
    assert panel._btn_sell_mkt._primary == "Sell 25% ≈150 @MKT"
    # Toggling back restores today's plain label.
    panel._btn_qty_unit.click()
    panel._refresh_action_labels()
    assert panel._btn_buy_mkt._primary == "Buy 100 @MKT"


def test_preset_and_reset_use_property_values(panel):
    panel._btn_qty_unit.click()                     # → %
    # Click the 50% chip (property-based, "50%" label can't int()-parse).
    chip = next(b for b in panel._qty_btn_group
                if b.property("qty_value") == 50)
    chip.click()
    assert panel._active_pct == 50
    assert panel._active_qty == 3892                # 50% → 2×1946
    panel._reset_qty_to_default()
    assert panel._active_pct == 25
    assert panel._active_qty == 1946
    # Back in shares, reset restores the share default.
    panel._btn_qty_unit.click()
    panel._reset_qty_to_default()
    assert panel._active_qty == 100
    assert panel._qty_unit == UNIT_SH


def test_share_labels_show_total_across_accounts(panel):
    # SH mode: the buttons show qty × checked accounts (the fan-out
    # places one order per account) — 100 × 2 accounts → 200.
    panel.set_percent_context(
        lambda _sym: [("A", 1.0, 0.0, 0.0), ("B", 1.0, 0.0, 0.0)])
    panel.refresh_percent_preview()
    assert panel._btn_buy_mkt._primary == "Buy 200 @MKT"
    assert panel._btn_sell_mkt._primary == "Sell 200 @MKT"
    # Payload qty stays per-account.
    payloads = []
    panel.order_requested.connect(payloads.append)
    panel._emit_order("BUY", "MKT")
    assert payloads[-1]["qty"] == 100


def test_price_tick_recomputes_percent(panel):
    panel._btn_qty_unit.click()
    assert panel._active_qty == 1946
    # Price doubles → half the shares.
    panel._on_bg_price_update("NVDA", 364.80, 364.70, 364.90)
    assert panel._active_qty == 973
