"""Micro-benchmark for the hot data paths that degrade over a session.

Run:  python scripts/bench_perf.py

Synthesizes a day's worth of fills/orders and times the operations the
GUI thread performs constantly: painting the Trades summary (data-row
and footer sweeps), absorbing price ticks, the 1 Hz status-bar
``pnl_totals``, and bulk fill/order ingestion.  Timings print as a
table; re-run after each performance phase and compare.

Baseline recorded on 2026-08-19 (commit 7425811, pre-overhaul),
N=5000 fills / 8 symbols / 4 accounts / R=8 rows (TICKS=100 because a
single pre-overhaul footer repaint costs ~0.9 s at this N):

    5000-fill summary load                       35.4 ms
    full summary sweep (rows+footer)           2196.1 ms
    footer-only sweep                           943.6 ms
    100 x set_last_price (+footer repaint)    94179.4 ms
    pnl_totals x 10                              79.2 ms
    5000-fill TradesModel load                  440.5 ms
    2000-order OrderStatusModel load            152.3 ms
"""
from __future__ import annotations

import os
import random
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

N_FILLS = 5000
N_SYMBOLS = 8
N_ACCOUNTS = 4
DAY = "2026-08-19"

_SYMBOLS = [f"SY{i}" for i in range(N_SYMBOLS)]
_ACCOUNTS = [f"U{i:03d}" for i in range(N_ACCOUNTS)]


def make_fills(n: int) -> list[dict]:
    rng = random.Random(42)
    fills = []
    for i in range(n):
        sym = _SYMBOLS[i % N_SYMBOLS]
        secs = 9 * 3600 + 30 * 60 + i * 5  # every 5s from 09:30
        t = f"{DAY}T{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
        fills.append({
            "exec_id": f"e{i}",
            "con_id": (i % N_SYMBOLS) + 1,
            "symbol": sym,
            "local_symbol": sym,
            "sec_type": "STK",
            "side": "BOT" if rng.random() < 0.5 else "SLD",
            "shares": rng.choice([1, 2, 5, 10]),
            "price": round(100 + rng.random() * 10, 2),
            "exchange": "NASDAQ",
            "time_sort": t,
            "account_id": _ACCOUNTS[i % N_ACCOUNTS],
            "commission": 0.35,
        })
    return fills


def timed(label: str, fn, *, repeat: int = 1) -> float:
    t0 = time.perf_counter()
    for _ in range(repeat):
        fn()
    ms = (time.perf_counter() - t0) * 1000
    print(f"  {label:<42} {ms:10.1f} ms")
    return ms


def sweep(model, rows: range | None = None) -> None:
    """Emulate a repaint: the role set the delegates pull per cell."""
    roles = (Qt.ItemDataRole.DisplayRole,
             Qt.ItemDataRole.DisplayRole,      # delegates fetch Display twice
             Qt.ItemDataRole.ForegroundRole)
    from src.ui.widgets.table_theme import SortValueRole
    rows = rows if rows is not None else range(model.rowCount())
    cols = model.columnCount()
    for r in rows:
        for c in range(cols):
            idx = model.index(r, c)
            for role in roles:
                model.data(idx, role)
            model.data(idx, SortValueRole)


def bench_summary() -> None:
    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    from datetime import date
    pinned = date(2026, 8, 19)
    tfp.today_et = lambda: pinned
    tsm.today_et = lambda: pinned

    from src.ui.widgets.trades_summary_model import TradesSummaryModel
    fills = make_fills(N_FILLS)

    m = TradesSummaryModel()
    timed(f"{N_FILLS}-fill summary load", lambda: [m.add_fill(f) for f in fills])
    for s in _SYMBOLS:
        m.set_last_price(s, 105.0)

    n_rows = m.rowCount()
    print(f"  (summary rows incl. footer: {n_rows})")
    timed("full summary sweep (rows+footer)", lambda: sweep(m))
    timed("footer-only sweep", lambda: sweep(m, range(n_rows - 1, n_rows)))

    n_ticks = int(os.environ.get("TICKS", "100"))
    prices = [105.0 + (i % 40) * 0.01 for i in range(n_ticks)]

    def ticks():
        for i, p in enumerate(prices):
            m.set_last_price(_SYMBOLS[i % N_SYMBOLS], p)
            # emulate the repaint each dataChanged provokes: the hit row
            # plus the footer, at delegate role counts
            sweep(m, range(n_rows - 1, n_rows))

    timed(f"{n_ticks} x set_last_price (+footer repaint)", ticks)
    timed("pnl_totals x 10", lambda: [m.pnl_totals() for _ in range(10)])


def bench_flat() -> None:
    from src.ui.widgets.trades_model import TradesModel
    fills = make_fills(N_FILLS)
    m = TradesModel()
    timed(f"{N_FILLS}-fill TradesModel load", lambda: [m.add_fill(f) for f in fills])


def bench_orders() -> None:
    from src.ui.widgets.order_status_model import OrderStatusModel
    m = OrderStatusModel()

    def load():
        for i in range(2000):
            m.add_order(f"U001_o{i}", {
                "order_ref": f"nano_260819_{i:06d}_abcd",
                "symbol": "SY0", "account": "U001", "action": "BUY",
                "order_type": "LMT", "quantity": "1", "limit_price": "100",
                "stop_price": "-", "tif": "DAY", "fill_price": "-",
                "status": "Submitted", "time": "10:00:00",
                "time_sort": f"2026-08-19T10:{(i // 60) % 60:02d}:{i % 60:02d}",
            })

    timed("2000-order OrderStatusModel load", load)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    print(f"bench_perf: N={N_FILLS} fills, {N_SYMBOLS} symbols, "
          f"{N_ACCOUNTS} accounts")
    bench_summary()
    bench_flat()
    bench_orders()


if __name__ == "__main__":
    main()
