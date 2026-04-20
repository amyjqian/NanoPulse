"""Paper-account integration test for IBWorkerThread._do_close_tier.

Exercises the Take Profit / Stop Loss header-button flow end-to-end:
  1. Opens a long MNQ position (BUY 2 MKT).
  2. Scale-out: close_tier TP with req_qty=1 → SELL LMT qty=1 above mkt.
  3. Scale-out: close_tier SL with req_qty=1 → SELL STP qty=1 below mkt.
  4. Cancels #2 and #3 (far from market, won't fill).
  5. Full close: close_tier TP with req_qty=10 → SELL LMT qty=2 (capped).
  6. Cancels #5.
  7. Flattens the position (SELL 2 MKT).
  8. Zero-position no-op: close_tier again → no new order.
  9. Short side: SELL 1 MKT, close_tier TP → BUY LMT below mkt.
 10. Cleans up (cancel the pending BUY LMT, BUY 1 MKT to flatten short).

Run:
    PYTHONPATH=. python scripts/paper_test_close_tier.py

Exit code 0 on pass, 1 on failure.
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime

from ib_async import IB, Future, LimitOrder, MarketOrder

from src.core.ib_worker import IBWorkerThread
from src.core.reference_data import get_instance as _rd

HOST = "127.0.0.1"
PORT = 7403
CLIENT_ID = 8300 + random.randint(0, 500)


def _log(m): print(f"[{datetime.now():%H:%M:%S}] {m}")


async def _find_mnq(ib: IB) -> Future:
    now = datetime.now()
    for o in range(6):
        m = now.month + o
        y = now.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        if m not in (3, 6, 9, 12):
            continue
        c = Future("MNQ", f"{y}{m:02d}", "CME", currency="USD")
        q = await ib.qualifyContractsAsync(c)
        if q and q[0].conId:
            return q[0]
    raise RuntimeError("no MNQ contract")


async def _get_last_price(ib: IB, contract) -> float:
    t = ib.reqMktData(contract, "", False, False)
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            for p in (t.last, t.close, t.marketPrice()):
                if p and p > 0:
                    return float(p)
    finally:
        ib.cancelMktData(contract)
    raise RuntimeError("no MNQ price")


async def _wait_filled(trade, timeout_s: float = 5.0) -> bool:
    elapsed = 0.0
    while elapsed < timeout_s:
        if trade.orderStatus.status in ("Filled", "Completed"):
            return True
        if trade.orderStatus.status in ("Cancelled", "Inactive",
                                        "ApiCancelled"):
            return False
        await asyncio.sleep(0.1)
        elapsed += 0.1
    return trade.orderStatus.status in ("Filled", "Completed")


def _make_worker_stub(ib: IB) -> IBWorkerThread:
    """Bare IBWorkerThread with just the attributes _do_close_tier needs."""
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = ib
    wt._label = "paper-close-tier"
    wt._trades = {}
    wt._has_status_handler = set()
    wt._portfolio_cache = {}
    wt._order_times = {}
    wt._order_error_messages = {}
    # _emit_order_status and _apply_fill_to_position don't need to do
    # anything during the test — they're exercised by unit tests.
    wt._emit_order_status = lambda *a, **kw: None
    wt._apply_fill_to_position = lambda *a, **kw: None
    return wt


async def _open_position(ib: IB, contract, action: str, qty: int,
                         account: str) -> bool:
    """Submit a MKT order to open or flatten a position.  Returns True
    on fill."""
    order = MarketOrder(action, qty)
    order.account = account
    order.tif = "DAY"
    trade = ib.placeOrder(contract, order)
    _log(f"  placed {action} {qty} MKT → {trade.order.orderId}")
    ok = await _wait_filled(trade, timeout_s=6.0)
    if ok:
        _log(f"  filled at {trade.orderStatus.avgFillPrice}")
    else:
        _log(f"  not filled, status={trade.orderStatus.status}")
    return ok


async def _current_position(ib: IB, contract, account: str) -> float:
    await asyncio.sleep(0.5)   # let positions event catch up
    for p in ib.positions():
        if p.account == account and p.contract.conId == contract.conId:
            return float(p.position)
    return 0.0


async def _scenario_long(ib, contract, account, last, wt) -> list[str]:
    fails: list[str] = []
    _log("══════ LONG scenario ══════")

    # Open long 2
    if not await _open_position(ib, contract, "BUY", 2, account):
        fails.append("could not open long position — market closed?")
        return fails
    pos = await _current_position(ib, contract, account)
    _log(f"  position after open = {pos:+.0f}")
    if pos != 2.0:
        fails.append(f"expected position=+2, got {pos}")

    # 1) Scale-out TP: req=1 → qty=1 LMT above market
    _log("\n--- TP scale-out (req=1) ---")
    placed_before = len(list(wt._trades.keys()))
    await wt._do_close_tier("MNQ", account, "tp", offset_pts=100.0,
                            requested_qty=1, current_price=last,
                            cap_offset=0.0)
    await asyncio.sleep(0.5)
    new_trades = list(wt._trades.values())[placed_before:]
    if len(new_trades) != 1:
        fails.append(
            f"TP scale-out: expected 1 trade submitted, got {len(new_trades)}")
    else:
        t = new_trades[0]
        o = t.order
        _log(f"  → {o.action} {o.totalQuantity} {o.orderType} @ "
             f"{getattr(o,'lmtPrice','—')}  (status={t.orderStatus.status})")
        if o.action != "SELL":
            fails.append(f"TP scale-out action={o.action} expected SELL")
        if o.totalQuantity != 1:
            fails.append(
                f"TP scale-out qty={o.totalQuantity} expected 1 "
                f"(min of pos=2, req=1)")
        if o.orderType != "LMT":
            fails.append(f"TP scale-out orderType={o.orderType} expected LMT")
        if o.lmtPrice <= last:
            fails.append(
                f"TP long lmt={o.lmtPrice} should be > last={last}")
        if t.orderStatus.status in ("Cancelled", "Inactive",
                                     "ApiCancelled", "ValidationError"):
            fails.append(
                f"TWS rejected the order: status={t.orderStatus.status}")
        ib.cancelOrder(o)

    # 2) Scale-out SL: req=1 → qty=1 STP below market
    _log("\n--- SL scale-out (req=1) ---")
    placed_before = len(list(wt._trades.keys()))
    await wt._do_close_tier("MNQ", account, "sl", offset_pts=100.0,
                            requested_qty=1, current_price=last,
                            cap_offset=0.0)
    await asyncio.sleep(0.5)
    new_trades = list(wt._trades.values())[placed_before:]
    if len(new_trades) != 1:
        fails.append(
            f"SL scale-out: expected 1 trade, got {len(new_trades)}")
    else:
        t = new_trades[0]
        o = t.order
        _log(f"  → {o.action} {o.totalQuantity} {o.orderType} aux="
             f"{getattr(o,'auxPrice','—')}  (status={t.orderStatus.status})")
        if o.action != "SELL":
            fails.append(f"SL scale-out action={o.action} expected SELL")
        if o.totalQuantity != 1:
            fails.append(f"SL scale-out qty={o.totalQuantity} expected 1")
        if o.orderType != "STP":
            fails.append(f"SL scale-out orderType={o.orderType} expected STP")
        if o.auxPrice >= last:
            fails.append(
                f"SL long stop={o.auxPrice} should be < last={last}")
        if t.orderStatus.status in ("Cancelled", "Inactive",
                                     "ApiCancelled", "ValidationError"):
            fails.append(
                f"TWS rejected the order: status={t.orderStatus.status}")
        ib.cancelOrder(o)

    # 3) Full-close TP: req=10 → qty=2 (min(|pos|=2, req=10))
    _log("\n--- TP full-close (req=10, pos=2) ---")
    placed_before = len(list(wt._trades.keys()))
    await wt._do_close_tier("MNQ", account, "tp", offset_pts=100.0,
                            requested_qty=10, current_price=last,
                            cap_offset=0.0)
    await asyncio.sleep(0.5)
    new_trades = list(wt._trades.values())[placed_before:]
    if len(new_trades) != 1:
        fails.append(f"TP full-close: expected 1 trade, got {len(new_trades)}")
    else:
        t = new_trades[0]
        o = t.order
        _log(f"  → {o.action} {o.totalQuantity} {o.orderType} @ "
             f"{getattr(o,'lmtPrice','—')}")
        if o.totalQuantity != 2:
            fails.append(
                f"TP full-close qty={o.totalQuantity} expected 2 "
                f"(capped at position size)")
        if t.orderStatus.status in ("Cancelled", "Inactive",
                                     "ApiCancelled", "ValidationError"):
            fails.append(
                f"TWS rejected the order: status={t.orderStatus.status}")
        ib.cancelOrder(o)

    await asyncio.sleep(0.8)  # let cancels propagate

    # Flatten the long
    _log("\n--- flatten long ---")
    await _open_position(ib, contract, "SELL", 2, account)
    pos_after = await _current_position(ib, contract, account)
    _log(f"  position after flatten = {pos_after:+.0f}")
    if pos_after != 0.0:
        fails.append(f"expected flat after SELL 2, got {pos_after}")

    # 4) Zero-position no-op
    _log("\n--- zero-position no-op ---")
    placed_before = len(list(wt._trades.keys()))
    await wt._do_close_tier("MNQ", account, "tp", offset_pts=100.0,
                            requested_qty=5, current_price=last,
                            cap_offset=0.0)
    await asyncio.sleep(0.3)
    new_trades = list(wt._trades.values())[placed_before:]
    if new_trades:
        fails.append(
            f"zero-position should be no-op, but {len(new_trades)} "
            f"order(s) were submitted")
    else:
        _log("  ✓ no order submitted for zero position")

    return fails


async def _scenario_short(ib, contract, account, last, wt) -> list[str]:
    fails: list[str] = []
    _log("\n══════ SHORT scenario ══════")

    if not await _open_position(ib, contract, "SELL", 1, account):
        fails.append("could not open short position")
        return fails
    pos = await _current_position(ib, contract, account)
    _log(f"  position after open = {pos:+.0f}")
    if pos != -1.0:
        fails.append(f"expected position=-1, got {pos}")

    _log("\n--- TP short (expect BUY LMT below mkt) ---")
    placed_before = len(list(wt._trades.keys()))
    await wt._do_close_tier("MNQ", account, "tp", offset_pts=100.0,
                            requested_qty=5, current_price=last,
                            cap_offset=0.0)
    await asyncio.sleep(0.5)
    new_trades = list(wt._trades.values())[placed_before:]
    if len(new_trades) != 1:
        fails.append(f"TP short: expected 1 trade, got {len(new_trades)}")
    else:
        t = new_trades[0]
        o = t.order
        _log(f"  → {o.action} {o.totalQuantity} {o.orderType} @ "
             f"{getattr(o,'lmtPrice','—')}")
        if o.action != "BUY":
            fails.append(f"TP short action={o.action} expected BUY")
        if o.totalQuantity != 1:
            fails.append(
                f"TP short qty={o.totalQuantity} expected 1 "
                f"(min of |pos|=1, req=5)")
        if o.lmtPrice >= last:
            fails.append(
                f"TP short lmt={o.lmtPrice} should be < last={last}")
        if t.orderStatus.status in ("Cancelled", "Inactive",
                                     "ApiCancelled", "ValidationError"):
            fails.append(
                f"TWS rejected the order: status={t.orderStatus.status}")
        ib.cancelOrder(o)

    await asyncio.sleep(0.8)
    _log("\n--- flatten short ---")
    await _open_position(ib, contract, "BUY", 1, account)
    pos_after = await _current_position(ib, contract, account)
    _log(f"  position after flatten = {pos_after:+.0f}")
    if pos_after != 0.0:
        fails.append(f"expected flat after BUY 1, got {pos_after}")

    return fails


async def run() -> int:
    # The worker's cache-based exchange lookup reads from ReferenceData,
    # which is populated by the main app on startup.  This standalone
    # test process has to load it explicitly so close_tier can resolve
    # MNQ's exchange without making a TWS round-trip.
    _rd().reload_contracts()
    mnq_rows = _rd().contracts_by_symbol.get("MNQ", [])
    _log(f"loaded {len(mnq_rows)} MNQ contract row(s) from local cache "
         f"(exchange={mnq_rows[0].get('exchange', '?') if mnq_rows else 'n/a'})")

    ib = IB()
    _log(f"Connecting {HOST}:{PORT} clientId={CLIENT_ID}")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    _log(f"Connected, server v{ib.client.serverVersion()}")

    try:
        contract = await _find_mnq(ib)
        last = await _get_last_price(ib, contract)
        _log(f"MNQ {contract.localSymbol} last ≈ {last:.2f}")

        accounts = ib.managedAccounts()
        if not accounts:
            _log("no managed accounts — aborting")
            return 1
        account = accounts[0]
        _log(f"account = {account}")

        # Clean baseline: cancel all open orders for this client and
        # flatten MNQ on this account so the test sees pos=0 at the start.
        try:
            for t in list(ib.openTrades()):
                if t.order.clientId == CLIENT_ID:
                    ib.cancelOrder(t.order)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        existing = await _current_position(ib, contract, account)
        if existing != 0:
            _log(f"flattening leftover position {existing:+.0f} before test")
            flat_action = "SELL" if existing > 0 else "BUY"
            await _open_position(
                ib, contract, flat_action, abs(int(existing)), account)
            await asyncio.sleep(0.5)
            existing = await _current_position(ib, contract, account)
            _log(f"position after flatten = {existing:+.0f}")

        wt = _make_worker_stub(ib)

        long_fails = await _scenario_long(ib, contract, account, last, wt)
        short_fails = await _scenario_short(ib, contract, account, last, wt)

        _log("\n══════ SUMMARY ══════")
        if long_fails:
            _log("LONG failures:")
            for f in long_fails:
                _log(f"  · {f}")
        if short_fails:
            _log("SHORT failures:")
            for f in short_fails:
                _log(f"  · {f}")
        if long_fails or short_fails:
            return 1
        _log("✅ close_tier verified end-to-end on paper account.")
        return 0

    finally:
        # Best-effort cancel of any still-open orders from this client.
        try:
            for t in list(ib.openTrades()):
                if t.order.clientId == CLIENT_ID:
                    ib.cancelOrder(t.order)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        ib.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
