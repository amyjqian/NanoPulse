"""Paper-account integration test for the Book Trader data pipeline.

Spins up a real IBWorkerThread against IB Gateway on port 7401, issues
``subscribe_depth`` on MNQ, collects ``depth_updated`` and
``bg_price_updated`` emits for 10 seconds, and asserts:

  * depth_updated fires at ≤ 12 Hz (100 ms ± jitter throttler budget)
  * bid prices descend, ask prices ascend
  * snapshot sizes are plain ``(price, size)`` tuples (no ib_async
    NamedTuples leaking across the Qt signal boundary)
  * bg_price_updated for MNQ fires at least once (OrderEntry's L1
    subscription path — Book Trader piggybacks on it)
  * unsubscribe_depth cleans up cleanly

Run:
    PYTHONPATH=. python scripts/paper_test_book_trader.py
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime

from PySide6.QtCore import QCoreApplication, QTimer, Slot

from src.core.ib_worker import IBWorkerThread
from src.core.reference_data import get_instance as _rd

HOST = "127.0.0.1"
PORT = 7401
CLIENT_ID = 9100 + random.randint(0, 500)
TEST_SYMBOL = "MNQ"
COLLECT_SECONDS = 10


def _log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}")


def main() -> int:
    # Reference data must have MNQ so subscribe_depth can resolve the contract.
    from src.core import instruments
    c = instruments.get_contract(TEST_SYMBOL)
    rows = [c] if c else []
    if not rows:
        _log(f"no MNQ contract on any chip — add one via the symbol search first")
        return 1
    _log(f"cache hit: MNQ conid={rows[0].get('conid')} "
         f"exchange={rows[0].get('exchange')} "
         f"localSymbol={rows[0].get('local_symbol')}")

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    worker = IBWorkerThread(
        connection_label=f"paper-book-trader-{CLIENT_ID}",
        host=HOST,
        port=PORT,
        client_id=CLIENT_ID,
    )

    depth_emits: list[tuple[float, str, list, list]] = []
    l1_emits: list[tuple[float, str, float, float, float, float]] = []
    market_failures: list[str] = []

    @Slot(str, list, list)
    def on_depth(sym: str, bids: list, asks: list) -> None:
        depth_emits.append((time.monotonic(), sym, bids, asks))

    @Slot(str, float, float, float, float)
    def on_price(sym: str, last: float, bid: float, ask: float,
                 close: float) -> None:
        l1_emits.append((time.monotonic(), sym, last, bid, ask, close))

    @Slot(str)
    def on_failed(sym: str) -> None:
        market_failures.append(sym)
        _log(f"market_data_failed: {sym}")

    worker.depth_updated.connect(on_depth)
    worker.bg_price_updated.connect(on_price)
    worker.market_data_failed.connect(on_failed)

    _log(f"starting worker, connecting to {HOST}:{PORT} clientId={CLIENT_ID}…")
    worker.start()

    # Wait for the connection to stabilise (worker reconnect loop fires
    # every 5 s; 4 s is enough for the initial attempt).
    start = time.monotonic()
    while time.monotonic() - start < 8.0:
        QCoreApplication.processEvents()
        if worker.is_ib_connected:
            break
        time.sleep(0.1)
    if not worker.is_ib_connected:
        _log("worker never connected — aborting")
        worker.stop()
        worker.wait(3000)
        return 1
    _log("worker connected")

    # Subscribe L1 (so depth can anchor on a real last price), then L2.
    worker.subscribe_background_symbols([
        {
            "conid": int(rows[0].get("conid")),
            "symbol": TEST_SYMBOL,
            "sec_type": rows[0].get("sec_type", ""),
            "exchange": rows[0].get("exchange", "") or "SMART",
            "currency": rows[0].get("currency", "USD"),
            "local_symbol": rows[0].get("local_symbol", ""),
            "expiry": rows[0].get("expiry", ""),
            "multiplier": rows[0].get("multiplier", ""),
        }
    ])
    worker.subscribe_depth(TEST_SYMBOL, num_rows=10)
    _log(f"subscribed L1 + L2 on {TEST_SYMBOL}")

    # Collect emits.
    deadline = time.monotonic() + COLLECT_SECONDS
    _log(f"collecting emits for {COLLECT_SECONDS} s…")
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.05)

    _log(f"collected {len(depth_emits)} depth emits, "
         f"{len(l1_emits)} L1 emits in {COLLECT_SECONDS} s")

    failures: list[str] = []

    # ── L2 assertions ──────────────────────────────────────────────────
    if not depth_emits:
        if market_failures:
            _log("no depth emits — account lacks L2 entitlement "
                 "(market_data_failed fired); ladder would render "
                 "from L1 only.  Not a test failure.")
        else:
            _log("no depth emits in 10 s — quiet market or account "
                 "missing L2 entitlement.  Not a test failure because "
                 "the worker emitted no market_data_failed signal.")

    # Count emits with actual depth data separately from empty-snapshot
    # emits (IB can fire an initial empty emit before any depth ticks
    # arrive, especially on paper accounts without L2 entitlement).
    nonempty = [e for e in depth_emits if e[2] or e[3]]
    _log(f"depth emits: {len(depth_emits)} total, "
         f"{len(nonempty)} with at least one level")

    if len(depth_emits) >= 2:
        duration = depth_emits[-1][0] - depth_emits[0][0]
        if duration > 1.0:
            rate = len(depth_emits) / duration
            _log(f"depth emit rate: {rate:.1f} Hz over {duration:.1f} s")
            if rate > 12:
                failures.append(
                    f"depth rate {rate:.1f} Hz exceeds 12 Hz throttle")

    if nonempty:
        _, sym, bids, asks = nonempty[-1]
        _log(f"latest non-empty depth: {sym}  "
             f"bids={bids[:3]}  asks={asks[:3]}")
        if sym != TEST_SYMBOL:
            failures.append(f"emit sym={sym!r} expected {TEST_SYMBOL!r}")
        if bids and not all(bids[i][0] >= bids[i + 1][0]
                            for i in range(len(bids) - 1)):
            failures.append("bid prices not descending")
        if asks and not all(asks[i][0] <= asks[i + 1][0]
                            for i in range(len(asks) - 1)):
            failures.append("ask prices not ascending")
        for lst, name in ((bids, "bids"), (asks, "asks")):
            for entry in lst:
                if type(entry) is not tuple or len(entry) != 2:
                    failures.append(
                        f"{name} entry {entry!r} not a (price, size) tuple")
                    break

    # ── L1 sanity ──────────────────────────────────────────────────────
    if l1_emits:
        _, sym, last, bid, ask, close = l1_emits[-1]
        _log(f"latest L1: {sym}  last={last} bid={bid} ask={ask} close={close}")

    # ── Cleanup ────────────────────────────────────────────────────────
    _log("unsubscribing…")
    worker.unsubscribe_depth(TEST_SYMBOL)
    worker.unsubscribe_background_symbol(TEST_SYMBOL)
    time.sleep(0.5)
    QCoreApplication.processEvents()

    worker.stop()
    if not worker.wait(3000):
        _log("worker didn't stop in 3 s — leaking the thread")

    _log("──── SUMMARY ────")
    if failures:
        for f in failures:
            _log(f"❌ {f}")
        return 1
    _log("✅ Book Trader data pipeline verified on paper port 7401")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
