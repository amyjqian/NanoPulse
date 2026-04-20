"""Paper-account smoke test for STP LMT stop-loss children.

Two scenarios: BUY parent + SL cap (→ SELL STP LMT, aux > lmt) and SELL
parent + SL cap (→ BUY STP LMT, aux < lmt).  Each places parent + SL child
at wide offsets, verifies TWS accepts the STP LMT with the right shape,
then cancels.
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime

from ib_async import IB, Future, LimitOrder

from src.core.ib_worker import IBWorkerThread, _generate_order_ref, _round_to_tick

HOST, PORT = "127.0.0.1", 7402
CLIENT_ID = 8600 + random.randint(0, 500)


def _log(m): print(f"[{datetime.now():%H:%M:%S}] {m}")


async def _mnq(ib: IB) -> Future:
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


async def _last(ib: IB, c) -> float:
    t = ib.reqMktData(c, "", False, False)
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            for p in (t.last, t.close, t.marketPrice()):
                if p and p > 0:
                    return float(p)
    finally:
        ib.cancelMktData(c)
    raise RuntimeError("no price")


def _wt(ib):
    w = IBWorkerThread.__new__(IBWorkerThread)
    w._ib = ib
    w._label = "sl-stplmt"
    return w


async def _scenario(ib, contract, last, action):
    _log(f"━━━━━ {action} parent + SL w/ cap ━━━━━")
    fails = []
    if action == "BUY":
        limit = _round_to_tick(last - 200.0, 0.25)
    else:
        limit = _round_to_tick(last + 200.0, 0.25)

    parent = LimitOrder(action, 1, limit)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False
    parent.account = ib.managedAccounts()[0]
    parent.tif = "DAY"
    parent.orderRef = _generate_order_ref()
    parent.outsideRth = False
    _log(f"parent {action} LMT @ {limit:.2f}  orderId={parent.orderId}")

    params = {
        "sec_type": "FUT", "symbol": "MNQ",
        "account": parent.account, "tick_size": 0.25,
        "bracket_ref_price": limit,
        "limit_cap_offset": 0.50,  # 2 ticks for MNQ
        "stop_loss": {"value": 20, "unit": "ticks", "label": "20"},
    }

    wt = _wt(ib)
    kids = wt._build_bracket_children(
        parent, params, ref_price=limit, tick=0.25)
    if len(kids) != 1:
        fails.append(f"expected 1 child, got {len(kids)}")
        return fails
    ch = kids[0]
    if ch.orderType != "STP LMT":
        fails.append(f"orderType={ch.orderType} expected STP LMT")

    _log(f"child  {ch.orderType} {ch.action} "
         f"lmt={ch.lmtPrice} aux={ch.auxPrice} "
         f"parentId={ch.parentId} transmit={ch.transmit}")

    # IBKR STP LMT shape requirements
    if action == "BUY":
        if not (ch.auxPrice > ch.lmtPrice):
            fails.append(
                f"SELL STP LMT needs aux>lmt, got aux={ch.auxPrice} "
                f"lmt={ch.lmtPrice}")
    else:
        if not (ch.auxPrice < ch.lmtPrice):
            fails.append(
                f"BUY STP LMT needs aux<lmt, got aux={ch.auxPrice} "
                f"lmt={ch.lmtPrice}")

    pt = ib.placeOrder(contract, parent)
    ct = ib.placeOrder(contract, ch)
    await asyncio.sleep(2.0)

    _log(f"parent status={pt.orderStatus.status}")
    _log(f"child  status={ct.orderStatus.status} "
         f"parentId={ct.order.parentId}")

    if ct.orderStatus.status in ("Cancelled", "Inactive", "ApiCancelled"):
        fails.append(f"child rejected: {ct.orderStatus.status}")
    if ct.order.parentId != pt.order.orderId:
        fails.append(
            f"parentId mismatch: child={ct.order.parentId} "
            f"parent={pt.order.orderId}")

    ib.cancelOrder(pt.order)
    await asyncio.sleep(2.0)
    _log(f"after cancel: parent={pt.orderStatus.status} "
         f"child={ct.orderStatus.status}")
    if pt.orderStatus.status != "Cancelled":
        fails.append(f"parent not Cancelled: {pt.orderStatus.status}")
    if ct.orderStatus.status != "Cancelled":
        fails.append(f"child not auto-cancelled: {ct.orderStatus.status}")
    return fails


async def run():
    ib = IB()
    _log(f"Connecting {HOST}:{PORT} cid={CLIENT_ID}")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    _log(f"Connected v{ib.client.serverVersion()}")
    total = {}
    try:
        c = await _mnq(ib)
        last = await _last(ib, c)
        _log(f"MNQ last ≈ {last:.2f}")
        _log("")
        for action in ("BUY", "SELL"):
            fails = await _scenario(ib, c, last, action)
            if fails:
                total[action] = fails
            _log("")
        _log("━━━━━ SUMMARY ━━━━━")
        if total:
            for act, fs in total.items():
                _log(f"❌ {act}")
                for f in fs:
                    _log(f"    · {f}")
            return 1
        _log("✅ STP LMT stop-loss verified for BUY and SELL parents.")
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
