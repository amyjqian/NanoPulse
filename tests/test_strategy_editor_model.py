"""Quick smoke-test for StrategyPivotModel (no GUI needed)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.ui.widgets.strategy_editor as se

# Redirect DB to a temp file
_tmp = tempfile.mktemp(suffix=".db")
se._DB_PATH = Path(_tmp)

from src.ui.widgets.strategy_editor import StrategyPivotModel  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

m = StrategyPivotModel()
assert m.columnCount() == 33
assert m.rowCount() == 0

# Add
assert m.add_symbol("ES")
assert m.add_symbol("GC")
assert not m.add_symbol("es")     # duplicate normalised
assert m.rowCount() == 2
assert m.symbol_at(0) == "ES"

# setData on offset col
idx = m.index(0, 1)               # ES, TP T1 Off
m.setData(idx, 12.5, Qt.ItemDataRole.EditRole)
idx2 = m.index(0, 2)              # ES, TP T1 Val – should be blocked
assert not m.setData(idx2, 999.0, Qt.ItemDataRole.EditRole), \
    "Val col must be read-only"

# Save + reload
m.save_to_db()
m2 = StrategyPivotModel()
m2.load_from_db()
assert m2.rowCount() == 2
assert m2.symbol_at(0) == "ES"
assert m2._data["ES"][("take_profit", 1, "offset")] == 12.5

# Delete
m2.delete_symbol_at(0)
assert m2.rowCount() == 1
assert m2.symbol_at(0) == "GC"

print("All StrategyPivotModel tests PASSED")

# ── Multiplier tests ─────────────────────────────────────────────────────────
m4 = StrategyPivotModel()
m4.add_symbol("NQ")
m4.add_symbol("MNQ")

idx_off = m4.index(0, 1)          # NQ, TP T1 Off
m4.setData(idx_off, 10.0, Qt.ItemDataRole.EditRole)

# Without multiplier: display should be "—"
disp = m4.data(m4.index(0, 2), Qt.ItemDataRole.DisplayRole)  # TP T1 Val
assert disp == "—", f"Expected '—' without multiplier, got {disp!r}"

# Set multipliers
m4.set_multipliers({"NQ": 20.0, "MNQ": 2.0})

# Val = offset(10) × multiplier(20) = 200
disp2 = m4.data(m4.index(0, 2), Qt.ItemDataRole.DisplayRole)
assert disp2 == "200.00", f"Expected '200.00', got {disp2!r}"

# Changing offset should change val display
m4.setData(idx_off, 5.0, Qt.ItemDataRole.EditRole)
disp3 = m4.data(m4.index(0, 2), Qt.ItemDataRole.DisplayRole)
assert disp3 == "100.00", f"Expected '100.00', got {disp3!r}"

# Val column must be non-editable via flags
flags = m4.flags(m4.index(0, 2))
assert not (flags & Qt.ItemFlag.ItemIsEditable), "Val flag must not be editable"
# Offset column must be editable
flags_off = m4.flags(m4.index(0, 1))
assert flags_off & Qt.ItemFlag.ItemIsEditable, "Offset flag must be editable"

# save_to_db stores computed val
import sqlite3
m4.save_to_db()
conn = sqlite3.connect(se._DB_PATH)
row = conn.execute(
    "SELECT offset, value FROM strategy_tiers "
    "WHERE symbol='NQ' AND order_type='take_profit' AND tier_level=1"
).fetchone()
conn.close()
assert row is not None
assert row[0] == 5.0,   f"Stored offset mismatch: {row[0]}"
assert row[1] == 100.0, f"Stored val mismatch (5×20=100): {row[1]}"

print("All multiplier tests PASSED")

# ── Copy / Paste / Duplicate tests ───────────────────────────────────────────
m3 = StrategyPivotModel()
m3.add_symbol("NQ")
m3.add_symbol("MNQ")
m3.set_multipliers({"NQ": 20.0, "MNQ": 2.0})

idx = m3.index(0, 5)              # NQ, TP T3 Off  (col 5)
m3.setData(idx, 42.0, Qt.ItemDataRole.EditRole)

# copy_row_data
data_copy = m3.copy_row_data(0)
assert data_copy is not None
assert data_copy[("take_profit", 3, "offset")] == 42.0
# deep copy check
data_copy[("take_profit", 3, "offset")] = 0.0
assert m3._data["NQ"][("take_profit", 3, "offset")] == 42.0, "deep copy broken"

# insert_symbol_with_data
ok = m3.insert_symbol_with_data("NQ_COPY", data_copy)
assert ok, "insert_symbol_with_data returned False"
assert m3.rowCount() == 3
assert not m3.insert_symbol_with_data("NQ", {}), "duplicate should be rejected"

# rows_to_tsv / parse_tsv_rows round-trip
tsv = m3.rows_to_tsv([0])
parsed = StrategyPivotModel.parse_tsv_rows(tsv)
assert len(parsed) == 1
p_sym, p_data = parsed[0]
assert p_sym == "NQ"
assert p_data[("take_profit", 3, "offset")] == 42.0

# Multi-row TSV round-trip
tsv2 = m3.rows_to_tsv([0, 1])
parsed2 = StrategyPivotModel.parse_tsv_rows(tsv2)
assert len(parsed2) == 2
assert parsed2[0][0] == "NQ"
assert parsed2[1][0] == "MNQ"

# Malformed line skipped
bad_tsv = "BAD\tonly_two_cols\n" + tsv
parsed3 = StrategyPivotModel.parse_tsv_rows(bad_tsv)
assert len(parsed3) == 1, "bad line should be silently skipped"

os.unlink(_tmp)
print("All copy/paste/duplicate tests PASSED")



