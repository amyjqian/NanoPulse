"""
TradesFilterProxy – shared QSortFilterProxyModel for the flat trades
and summary views.

Filters on two axes:
  * Text search — case-insensitive substring match against a fixed set
    of underlying row keys (symbol / side / account / sec_type for the
    flat model; symbol only for the summary model).
  * Date range — keep rows whose ``time_sort`` ISO timestamp is within
    the last *N* trading days (ET).  The summary model has no per-fill
    timestamp, so date filtering is a no-op on it; the flat view is the
    intended target.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Keys the search box matches, per underlying model type.
# The proxy asks the source model for ``row_data(row)`` and matches these.
_SEARCH_KEYS_FLAT = ("symbol", "side", "account", "sec_type")
_SEARCH_KEYS_SUMMARY = ("symbol",)


class TradesFilterProxy(QSortFilterProxyModel):
    """Proxy model wiring the control-bar search box + date range to the
    flat/summary trades sources."""

    def __init__(self, parent=None, *, is_summary: bool = False):
        super().__init__(parent)
        self._is_summary = is_summary
        self._search: str = ""
        self._days_back: int = 1  # "Today" by default

    # ── Public setters (hooked to the control bar) ──────────────────────────

    def setSearchText(self, text: str) -> None:
        new_val = (text or "").strip().lower()
        if new_val == self._search:
            return
        self._search = new_val
        self.invalidateFilter()

    def setDaysBack(self, days: int) -> None:
        """1 = today, 2 = last 2 days, …, 7 = last 7 days."""
        days = max(1, int(days or 1))
        if days == self._days_back:
            return
        self._days_back = days
        self.invalidateFilter()

    # ── Core filter ─────────────────────────────────────────────────────────

    def filterAcceptsRow(self, src_row: int,
                         src_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None or src_parent.isValid():
            return True

        row_data = getattr(model, "row_data", None)
        data = row_data(src_row) if row_data else None
        if data is None:
            return True

        # Search filter
        if self._search:
            keys = _SEARCH_KEYS_SUMMARY if self._is_summary else _SEARCH_KEYS_FLAT
            if not any(self._search in str(data.get(k, "") or "").lower()
                       for k in keys):
                return False

        # Date filter – flat model only
        if not self._is_summary and self._days_back > 0:
            time_sort = data.get("time_sort", "")
            if time_sort and not self._within_days(time_sort, self._days_back):
                return False

        return True

    @staticmethod
    def _within_days(iso_time: str, days_back: int) -> bool:
        try:
            ts = datetime.fromisoformat(iso_time)
        except ValueError:
            return True  # malformed → don't filter out
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        local = ts.astimezone(_ET).date()
        today = datetime.now(_ET).date()
        cutoff: date = today - timedelta(days=days_back - 1)
        return local >= cutoff
