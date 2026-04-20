"""Paper-account integration test for the bracket-order flow.

Connects to IB Gateway/TWS on 127.0.0.1:7402 (paper), builds a parent LMT
BUY on MNQ plus TP/SL/Trail children via IBWorkerThread._build_bracket_children,
submits the whole bundle, verifies TWS accepted the linkage, then cancels
the parent to clean up (which auto-cancels the children).

Run:
    python scripts/paper_test_bracket.py

Exit code 0 on success, 1 on any assertion/timeout failure.
"""
from __future__ import annotations

import asyncio
import random
import sys
from types import SimpleNamespace
from datetime import datetime

from ib_async import IB, Future, LimitOrder

# Reuse the very code paths the UI uses.
from src.core.ib_worker import (
    IBWorkerThread, _generate_order_ref, _round_to_tick,
)

HOST = "127.0.0.1"
PORT = 7402
CLIENT_ID = 7777 + random.randint(0, 500)  # avoid collisions with NanoPulse itself


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


async def _find_mnq_contract(ib: IB) -> Future:
    """Qualify the front-month MNQ (Micro E-mini Nasdaq-100) contract.

    MNQ is liquid around the clock on Globex — good for a bracket smoke
    test because it's cheap and always has a market.
    """
    # Try a few recent quarter-ends; IB's front-month rolls quarterly.
    now = datetime.now()
    for month_offset in range(0, 6):
        m = now.month + month_offset
        y = now.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        if m not in (3, 6, 9, 12):
            continue
        expiry = f"{y}{m:02d}"
        c = Future(symbol="MNQ", lastTradeDateOrContractMonth=expiry,
                   exchange="CME", currency="USD")
        qualified = await ib.qualifyContractsAsync(c)
        if qualified and qualified[0].conId:
            return qualified[0]
    raise RuntimeError("Could not qualify an MNQ front-month contract")


async def _get_last_price(ib: IB, contract) -> float:
    """Request a snapshot and return the last/close price."""
    ticker = ib.reqMktData(contract, "", False, False)
    try:
        for _ in range(40):  # up to 4 seconds
            await asyncio.sleep(0.1)
            if ticker.last and ticker.last > 0:
                return float(ticker.last)
            if ticker.close and ticker.close > 0:
                return float(ticker.close)
            if ticker.marketPrice() and ticker.marketPrice() > 0:
                return float(ticker.marketPrice())
    finally:
        ib.cancelMktData(contract)
    raise RuntimeError("No MNQ price within 4 seconds")


def _build_worker_stub(ib: IB) -> IBWorkerThread:
    """Create an IBWorkerThread bare-bones enough that
    _build_bracket_children works with a real ib_async client."""
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = ib
    wt._label = "paper-test"
    return wt


async def run() -> int:
    ib = IB()
    _log(f"Connecting to {HOST}:{PORT} (clientId={CLIENT_ID})…")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    _log(f"Connected. Server version {ib.client.serverVersion()}")

    try:
        contract = await _find_mnq_contract(ib)
        _log(f"Contract: {contract.localSymbol} conId={contract.conId} "
             f"exchange={contract.exchange} mult={contract.multiplier}")

        last = await _get_last_price(ib, contract)
        _log(f"MNQ last ≈ {last:.2f}")

        # Park parent 200 points below market so it never fills.
        # MNQ tick = 0.25 points; multiplier = $2/pt.
        parent_limit = _round_to_tick(last - 200.0, 0.25)
        _log(f"Parent BUY LMT at {parent_limit:.2f} "
             f"({last - parent_limit:.2f} pts below mkt, will not fill)")

        # Build parent via the same API the worker uses.
        parent = LimitOrder("BUY", 1, parent_limit)
        parent.orderId = ib.client.getReqId()
        parent.transmit = False
        parent.account = ib.managedAccounts()[0]
        parent.tif = "DAY"
        parent.orderRef = _generate_order_ref()
        parent.outsideRth = False

        _log(f"Parent orderRef={parent.orderRef} orderId={parent.orderId} "
             f"account={parent.account}")

        # Tier dicts mirror what OrderEntryPanel emits via get_current_order_data().
        params = {
            "sec_type": "FUT",
            "symbol": "MNQ",
            "account": parent.account,
            "tick_size": 0.25,
            "limit_price": parent_limit,
            "profit_take": {"value": 40, "unit": "ticks",  "label": "40"},
            "stop_loss":   {"value": 20, "unit": "ticks",  "label": "20"},
            "trailing":    {"value": 10, "unit": "ticks",  "label": "10"},
        }

        wt = _build_worker_stub(ib)
        children = wt._build_bracket_children(
            parent, params, ref_price=parent_limit, tick=0.25)

        _log(f"Built {len(children)} children:")
        for ch in children:
            _log(f"  · {ch.orderType:<6} {ch.action} qty={ch.totalQuantity} "
                 f"lmt={getattr(ch,'lmtPrice','—')} "
                 f"aux={getattr(ch,'auxPrice','—')} "
                 f"parentId={ch.parentId} orderId={ch.orderId} "
                 f"oca={getattr(ch,'ocaGroup','') or '—'} "
                 f"transmit={ch.transmit} ref={ch.orderRef}")

        # Place parent, then children.
        parent_trade = ib.placeOrder(contract, parent)
        _log(f"Placed parent → trade.orderStatus={parent_trade.orderStatus.status}")

        child_trades = []
        for ch in children:
            t = ib.placeOrder(contract, ch)
            child_trades.append(t)
            _log(f"Placed child {ch.orderType} → status="
                 f"{t.orderStatus.status or 'pending'}")

        # Let TWS acknowledge the bundle.
        await asyncio.sleep(2.0)
        _log("---- Post-submission state ----")
        _log(f"Parent  orderId={parent_trade.order.orderId} "
             f"status={parent_trade.orderStatus.status} "
             f"filled={parent_trade.orderStatus.filled} "
             f"remaining={parent_trade.orderStatus.remaining}")
        for t in child_trades:
            _log(f"Child   orderId={t.order.orderId} "
                 f"type={t.order.orderType} "
                 f"parentId={t.order.parentId} "
                 f"status={t.orderStatus.status or '—'} "
                 f"oca={getattr(t.order,'ocaGroup','') or '—'}")

        # --- Verifications --------------------------------------------------
        failures = []
        if parent_trade.order.orderId <= 0:
            failures.append("parent orderId not assigned")
        for t in child_trades:
            if t.order.parentId != parent_trade.order.orderId:
                failures.append(
                    f"child {t.order.orderType} parentId={t.order.parentId} "
                    f"!= parent {parent_trade.order.orderId}")
            if t.orderStatus.status in ("Cancelled", "Inactive", "ApiCancelled"):
                failures.append(
                    f"child {t.order.orderType} was rejected: "
                    f"{t.orderStatus.status}")
        # No explicit OCA: attached children share parentId, which already
        # gives IBKR-level bracket OCA.  An explicit ocaGroup is rejected
        # as "OCA handling method mismatch" (error 201), so every child
        # must leave ocaGroup empty.
        for t in child_trades:
            if getattr(t.order, "ocaGroup", ""):
                failures.append(
                    f"unexpected ocaGroup on {t.order.orderType} child: "
                    f"{t.order.ocaGroup!r}")

        # --- Cleanup --------------------------------------------------------
        _log("Cancelling parent (TWS should auto-cancel the children)…")
        ib.cancelOrder(parent_trade.order)
        await asyncio.sleep(2.0)
        for t in child_trades:
            _log(f"Child post-cancel: orderId={t.order.orderId} "
                 f"type={t.order.orderType} status={t.orderStatus.status}")
        _log(f"Parent post-cancel: status={parent_trade.orderStatus.status}")

        # --- Result --------------------------------------------------------
        if failures:
            _log("\nFAILURES:")
            for f in failures:
                _log(f"  - {f}")
            return 1
        _log("\n✅ Bracket flow verified end-to-end against paper account.")
        return 0

    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
