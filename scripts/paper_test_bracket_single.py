"""Paper-account tests for single-child bracket flows.

Six scenarios — BUY and SELL parents × {TP only, SL only, Trail only}.
Each scenario:
  1. Parks a parent LMT 200 pts off market (won't fill),
  2. Builds the one enabled child via IBWorkerThread._build_bracket_children,
  3. Places parent + child,
  4. Asserts parentId linkage and child order type,
  5. Cancels the parent and verifies the child auto-cancels.

Run:
    PYTHONPATH=. python scripts/paper_test_bracket_single.py
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime

from ib_async import IB, Future, LimitOrder

from src.core.ib_worker import IBWorkerThread, _generate_order_ref, _round_to_tick

HOST = "127.0.0.1"
PORT = 7402
CLIENT_ID = 8100 + random.randint(0, 500)

# Scenarios: (label, parent_action, tier_key, tier_dict, expected_type)
SCENARIOS = [
    ("BUY  + TP only",    "BUY",  "profit_take",
     {"value": 40, "unit": "ticks", "label": "40"}, "LMT"),
    ("BUY  + SL only",    "BUY",  "stop_loss",
     {"value": 20, "unit": "ticks", "label": "20"}, "STP"),
    ("BUY  + Trail only", "BUY",  "trailing",
     {"value": 10, "unit": "ticks", "label": "10"}, "TRAIL"),
    ("SELL + TP only",    "SELL", "profit_take",
     {"value": 40, "unit": "ticks", "label": "40"}, "LMT"),
    ("SELL + SL only",    "SELL", "stop_loss",
     {"value": 20, "unit": "ticks", "label": "20"}, "STP"),
    ("SELL + Trail only", "SELL", "trailing",
     {"value": 10, "unit": "ticks", "label": "10"}, "TRAIL"),
]


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


async def _find_mnq_contract(ib: IB) -> Future:
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
    raise RuntimeError("Could not qualify MNQ front-month")


async def _get_last_price(ib: IB, contract) -> float:
    ticker = ib.reqMktData(contract, "", False, False)
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            for p in (ticker.last, ticker.close, ticker.marketPrice()):
                if p and p > 0:
                    return float(p)
    finally:
        ib.cancelMktData(contract)
    raise RuntimeError("No MNQ price within 4s")


def _build_worker_stub(ib: IB) -> IBWorkerThread:
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = ib
    wt._label = "paper-single"
    return wt


async def _run_scenario(ib: IB, contract, last: float,
                        label: str, action: str,
                        tier_key: str, tier_dict: dict,
                        expected_type: str) -> list[str]:
    """Run one scenario, return list of failure strings (empty = pass)."""
    _log(f"══════ {label} ══════")
    failures: list[str] = []

    # Park parent 200 pts off market so it never fills.
    if action == "BUY":
        parent_limit = _round_to_tick(last - 200.0, 0.25)
    else:
        parent_limit = _round_to_tick(last + 200.0, 0.25)

    parent = LimitOrder(action, 1, parent_limit)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False
    parent.account = ib.managedAccounts()[0]
    parent.tif = "DAY"
    parent.orderRef = _generate_order_ref()
    parent.outsideRth = False
    _log(f"parent {action} LMT @ {parent_limit:.2f} "
         f"(orderRef={parent.orderRef} orderId={parent.orderId})")

    params = {
        "sec_type": "FUT",
        "symbol": "MNQ",
        "account": parent.account,
        "tick_size": 0.25,
        "limit_price": parent_limit,
        tier_key: tier_dict,
    }

    wt = _build_worker_stub(ib)
    children = wt._build_bracket_children(
        parent, params, ref_price=parent_limit, tick=0.25)

    if len(children) != 1:
        failures.append(f"expected 1 child, got {len(children)}")
        return failures
    ch = children[0]

    # In-memory assertions before placing.
    expected_exit_action = "SELL" if action == "BUY" else "BUY"
    if ch.action != expected_exit_action:
        failures.append(
            f"child action={ch.action} expected {expected_exit_action}")
    if ch.orderType != expected_type:
        failures.append(
            f"child orderType={ch.orderType} expected {expected_type}")
    if ch.parentId != parent.orderId:
        failures.append(
            f"child parentId={ch.parentId} != parent orderId={parent.orderId}")
    if not ch.transmit:
        failures.append("sole child should have transmit=True")
    if getattr(ch, "ocaGroup", ""):
        failures.append(f"unexpected ocaGroup={ch.ocaGroup!r}")

    _log(f"child  {ch.orderType:<6} {ch.action} "
         f"lmt={getattr(ch,'lmtPrice','—')} "
         f"aux={getattr(ch,'auxPrice','—')} "
         f"parentId={ch.parentId} orderId={ch.orderId} "
         f"transmit={ch.transmit}")

    # Place parent + child.
    parent_trade = ib.placeOrder(contract, parent)
    child_trade  = ib.placeOrder(contract, ch)
    await asyncio.sleep(2.0)

    _log(f"parent status={parent_trade.orderStatus.status} "
         f"filled={parent_trade.orderStatus.filled} "
         f"remaining={parent_trade.orderStatus.remaining}")
    _log(f"child  status={child_trade.orderStatus.status} "
         f"parentId={child_trade.order.parentId}")

    if parent_trade.orderStatus.status in ("Cancelled", "Inactive",
                                           "ApiCancelled"):
        failures.append(
            f"parent rejected: {parent_trade.orderStatus.status}")
    if child_trade.orderStatus.status in ("Cancelled", "Inactive",
                                          "ApiCancelled"):
        failures.append(
            f"child rejected: {child_trade.orderStatus.status}")
    if child_trade.order.parentId != parent_trade.order.orderId:
        failures.append(
            f"TWS-reported child.parentId={child_trade.order.parentId} "
            f"!= parent.orderId={parent_trade.order.orderId}")

    # Cancel parent; expect TWS to auto-cancel the child.
    ib.cancelOrder(parent_trade.order)
    await asyncio.sleep(2.0)
    if parent_trade.orderStatus.status != "Cancelled":
        failures.append(
            f"parent not Cancelled after cancel: "
            f"{parent_trade.orderStatus.status}")
    if child_trade.orderStatus.status != "Cancelled":
        failures.append(
            f"child not auto-cancelled: {child_trade.orderStatus.status}")

    _log(f"after cancel: parent={parent_trade.orderStatus.status} "
         f"child={child_trade.orderStatus.status}")
    return failures


async def run() -> int:
    ib = IB()
    _log(f"Connecting to {HOST}:{PORT} (clientId={CLIENT_ID})…")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    _log(f"Connected. Server v{ib.client.serverVersion()}")

    total_failures: dict[str, list[str]] = {}
    try:
        contract = await _find_mnq_contract(ib)
        last = await _get_last_price(ib, contract)
        _log(f"MNQ last ≈ {last:.2f}  contract={contract.localSymbol}")
        _log("")

        for (label, action, tier_key, tier_dict, expected_type) in SCENARIOS:
            fails = await _run_scenario(ib, contract, last, label,
                                        action, tier_key, tier_dict,
                                        expected_type)
            if fails:
                total_failures[label] = fails
            _log("")

        _log("══════ SUMMARY ══════")
        if total_failures:
            for label, fails in total_failures.items():
                _log(f"❌ {label}")
                for f in fails:
                    _log(f"     · {f}")
            _log(f"\n{len(total_failures)}/{len(SCENARIOS)} scenarios failed")
            return 1
        _log(f"✅ All {len(SCENARIOS)} single-child scenarios verified "
             f"against paper account.")
        return 0

    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
