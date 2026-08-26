"""The post-snapshot commission back-fill (`_sweep_late_commissions`).

TWS streams a snapshot's commissionReports AFTER execDetailsEnd resolves
``reqExecutionsAsync``, so most fills are emitted commission-less — and
for fills replayed from previous sessions ib_async merges the late report
into ``ib.fills()`` WITHOUT firing ``commissionReportEvent`` (the wrapper
gates that emit on ``permId2Trade``, which only knows this session's own
live orders).  The sweep re-reads the merged cache and re-emits every
cached fill whose report has since arrived; the trade models dedupe by
execId, so each re-emit updates the row in place.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.ib_worker import IBWorkerThread

_UTC_TIME = datetime(2026, 8, 24, 14, 34, 54, tzinfo=timezone.utc)


def _fill(exec_id: str, *, commission: float | None) -> SimpleNamespace:
    execution = SimpleNamespace(
        execId=exec_id, time=_UTC_TIME, side="SLD", shares=1.0,
        price=29075.25, exchange="CME", orderRef="nano_260824_103454_sefc",
        acctNumber="U4444444", orderId=7, clientId=1,
        liquidation=0, lastLiquidity=1,
    )
    if commission is None:
        # ib_async's default-initialised report: execId="" = not merged.
        report = SimpleNamespace(execId="", commission=0.0, realizedPNL=0.0)
    else:
        report = SimpleNamespace(
            execId=exec_id, commission=commission, realizedPNL=-231.82)
    contract = SimpleNamespace(
        conId=793356225, symbol="MNQ", localSymbol="MNQU6", secType="FUT",
        multiplier="2", lastTradeDateOrContractMonth="20260918",
        strike=0.0, right="")
    return SimpleNamespace(
        contract=contract, execution=execution, commissionReport=report)


def _make_worker(qtbot, merged_fills):
    wt = IBWorkerThread("test")
    wt._running = True
    wt._ib = SimpleNamespace(
        isConnected=lambda: True,
        fills=lambda: list(merged_fills),
    )
    return wt


def test_sweep_reemits_once_report_merges(qtbot, monkeypatch):
    # ib.fills() already holds the merged copy (report populated) …
    merged = [_fill("exec-1", commission=0.61)]
    wt = _make_worker(qtbot, merged)
    # … but the snapshot emitted (and cached) the fill before the report
    # arrived, so the cached dict says commission=None.
    stale = wt._flatten_fill(_fill("exec-1", commission=None))
    assert stale["commission"] is None
    wt._fills_cache["exec-1"] = stale

    emitted: list[dict] = []
    wt.fill_updated.connect(emitted.append)

    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(wt._sweep_late_commissions())

    assert len(emitted) == 1
    assert emitted[0]["exec_id"] == "exec-1"
    assert emitted[0]["commission"] == 0.61
    assert emitted[0]["realized_pnl"] == -231.82
    # The cache is healed too — a late request_fills_snapshot replay
    # must not resurrect the commission-less dict.
    assert wt._fills_cache["exec-1"]["commission"] == 0.61


def test_sweep_exits_quietly_when_nothing_missing(qtbot, monkeypatch):
    merged = [_fill("exec-1", commission=0.61)]
    wt = _make_worker(qtbot, merged)
    wt._fills_cache["exec-1"] = wt._flatten_fill(merged[0])

    emitted: list[dict] = []
    wt.fill_updated.connect(emitted.append)

    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(wt._sweep_late_commissions())
    assert emitted == []


def test_sweep_leaves_unmerged_fills_for_the_next_pass(qtbot, monkeypatch):
    """A fill whose report never arrives keeps its blank cells — the
    sweep must not fabricate a 0.00 for it — and the sweep still ends at
    the deadline instead of spinning."""
    merged = [_fill("exec-1", commission=None)]     # never merges
    wt = _make_worker(qtbot, merged)
    wt._fills_cache["exec-1"] = wt._flatten_fill(merged[0])

    emitted: list[dict] = []
    wt.fill_updated.connect(emitted.append)

    # First call sets the deadline at t=0; the second loop check reads
    # t=40 → past the 30 s deadline, so exactly one pass runs.
    ticks = [0.0, 0.0]
    import src.core.ib_worker as ibw
    monkeypatch.setattr(ibw.time, "monotonic",
                        lambda: ticks.pop(0) if ticks else 40.0)

    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(wt._sweep_late_commissions())
    assert emitted == []
    assert wt._fills_cache["exec-1"]["commission"] is None
