"""Paper-account regression for the BID_LMT / ASK_LMT → attached bracket
path.

Before the fix, ``_emit_bid_ask_lmt_order`` built its payload without
``profit_take`` / ``stop_loss`` / ``trailing``, so the parent LMT went
out naked and the TP / SL children were never submitted.  This script
drives the same payload shape the Order Entry panel emits when the user
clicks *Buy @Bid Limit* or *Sell @Ask Limit* with TP and SL toggles on,
then asserts the worker actually placed the parent plus both children.

Run:
    PYTHONPATH=. python scripts/paper_test_bidlmt_bracket.py
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime

from ib_async import IB, Future

from src.core.ib_worker import IBWorkerThread
from src.core.reference_data import get_instance as _rd

HOST = "127.0.0.1"
PORT = 7403
CLIENT_ID = 8500 + random.randint(0, 500)


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


async def _get_last(ib: IB, contract) -> float:
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


def _make_worker_stub(ib: IB) -> IBWorkerThread:
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = ib
    wt._label = "bidlmt-bracket"
    wt._trades = {}
    wt._has_status_handler = set()
    wt._portfolio_cache = {}
    wt._order_times = {}
    wt._order_error_messages = {}
    wt._emit_order_status = lambda *a, **kw: None
    wt._apply_fill_to_position = lambda *a, **kw: None
    return wt


async def run() -> int:
    _rd().reload_contracts()
    mnq = _rd().contracts_by_symbol.get("MNQ", [])
    _log(f"loaded {len(mnq)} MNQ contract(s) from cache "
         f"(exchange={mnq[0].get('exchange', '?') if mnq else 'n/a'})")

    ib = IB()
    _log(f"Connecting {HOST}:{PORT} clientId={CLIENT_ID}")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    _log(f"Connected v{ib.client.serverVersion()}")

    try:
        contract = await _find_mnq(ib)
        last = await _get_last(ib, contract)
        _log(f"MNQ {contract.localSymbol} last ≈ {last:.2f}")

        account = ib.managedAccounts()[0]

        # Park the LMT parent 200 pts below market (BUY @Bid-like) so it
        # sits pending — the bracket children attach and wait.
        parent_limit = round((last - 200.0) * 4) / 4  # snap to 0.25 tick

        # Build the exact params shape FlashTradeView._on_order_requested
        # forwards to worker.place_order for a BID_LMT order with TP + SL
        # tiers configured.
        params = {
            "conid": contract.conId,
            "symbol": "MNQ",
            "sec_type": "FUT",
            "exchange": "CME",
            "currency": "USD",
            "local_symbol": contract.localSymbol,
            "expiry": contract.lastTradeDateOrContractMonth,
            "multiplier": contract.multiplier,
            "account": account,
            "action": "BUY",
            "qty": 1,
            "order_type": "LMT",
            "tif": "DAY",
            "limit_price": parent_limit,
            "last_price": last,
            "tick_size": 0.25,
            "bracket_ref_price": parent_limit,  # BID_LMT anchors on limit
            "limit_cap_offset": 0.0,
            "profit_take": {"value": 10, "unit": "price", "label": "10"},
            "stop_loss":   {"value": 20, "unit": "price", "label": "20"},
            "trailing":    None,
        }

        _log(f"parent BUY LMT @ {parent_limit:.2f} with TP +10 pts / "
             f"SL -20 pts (attached)")

        # Route through the full _do_place_order path so the same code
        # the real order-request signal takes is exercised.
        wt = _make_worker_stub(ib)
        await wt._do_place_order(params)

        await asyncio.sleep(2.0)

        # Expect exactly 3 trades registered (parent + TP + SL).
        trades = list(wt._trades.values())
        _log(f"\n_trades registered: {len(trades)}")
        for t in trades:
            o = t.order
            _log(f"  · {o.orderType:<7} {o.action} qty={o.totalQuantity} "
                 f"lmt={getattr(o,'lmtPrice','—'):<9} "
                 f"aux={getattr(o,'auxPrice','—'):<9} "
                 f"parentId={o.parentId}  status={t.orderStatus.status}")

        failures: list[str] = []
        if len(trades) != 3:
            failures.append(
                f"expected 3 trades (parent + TP + SL), got {len(trades)}")

        actions = {t.order.orderType: t.order for t in trades}
        if "LMT" not in actions:
            failures.append("missing parent/TP LMT order")
        # Identify parent vs TP: parent action=BUY, TP action=SELL.
        parent_order = next(
            (t.order for t in trades if t.order.action == "BUY"), None)
        tp_order = next(
            (t.order for t in trades
             if t.order.action == "SELL" and t.order.orderType == "LMT"),
            None)
        sl_order = next(
            (t.order for t in trades
             if t.order.action == "SELL" and t.order.orderType == "STP"),
            None)
        if parent_order is None:
            failures.append("no BUY parent found in _trades")
        if tp_order is None:
            failures.append("no SELL LMT take-profit found in _trades")
        if sl_order is None:
            failures.append("no SELL STP stop-loss found in _trades")

        if parent_order and tp_order:
            if tp_order.parentId != parent_order.orderId:
                failures.append(
                    f"TP.parentId={tp_order.parentId} "
                    f"!= parent.orderId={parent_order.orderId}")
            expected_tp = parent_limit + 10.0
            if abs(tp_order.lmtPrice - expected_tp) > 0.26:
                failures.append(
                    f"TP lmt={tp_order.lmtPrice} expected ≈{expected_tp}")
        if parent_order and sl_order:
            if sl_order.parentId != parent_order.orderId:
                failures.append(
                    f"SL.parentId={sl_order.parentId} "
                    f"!= parent.orderId={parent_order.orderId}")
            expected_sl = parent_limit - 20.0
            if abs(sl_order.auxPrice - expected_sl) > 0.26:
                failures.append(
                    f"SL aux={sl_order.auxPrice} expected ≈{expected_sl}")

        # Clean up: cancel the parent so TWS auto-cancels its children.
        if parent_order is not None:
            _log("\ncancelling parent (children auto-cancel)…")
            ib.cancelOrder(parent_order)
            await asyncio.sleep(2.0)
            for t in trades:
                _log(f"  · {t.order.orderType} final status="
                     f"{t.orderStatus.status}")

        _log("\n══════ SUMMARY ══════")
        if failures:
            for f in failures:
                _log(f"❌ {f}")
            return 1
        _log("✅ BID_LMT with TP + SL submits parent + both children "
             "on paper account.")
        return 0

    finally:
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
