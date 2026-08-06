"""Reconnect re-qualification — self-healing chip metadata.

Covers the needs-qualification predicate, browse-param guessing for
bare entries, and the serialized queue against a fake worker: qualify →
upsert → resubscribe signal, cross-talk filtering, failure handling,
and once-per-session dedupe.
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QObject, Signal

import src.core.config_manager as cm
import src.core.instruments as instruments
from src.core.instrument_qualifier import (
    InstrumentQualifier,
    guess_browse_params,
    needs_qualification,
)

_OCC = "QQQ   260807P00670000"


class _FakeWorker(QObject):
    browse_results = Signal(list)
    browse_failed = Signal(str)
    connection_status_changed = Signal(str, bool)
    is_ib_connected = True

    def __init__(self):
        super().__init__()
        self.browsed: list[dict] = []

    def isRunning(self) -> bool:
        return True

    def request_browse(self, params: dict) -> None:
        self.browsed.append(params)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "app_config-test.json"
    monkeypatch.setattr(cm, "_active_path", p)
    instruments.invalidate_cache()
    yield p
    instruments.invalidate_cache()


def _write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")
    instruments.invalidate_cache()


class TestPredicate:
    def test_bare_entry_needs_qualification(self):
        assert needs_qualification({"symbol": "SPCX"}) is True

    def test_qualified_stk_does_not(self):
        assert needs_qualification(
            {"symbol": "AAPL", "conid": "265598", "sec_type": "STK"}) is False

    def test_expired_fut_needs_requalification(self):
        assert needs_qualification(
            {"symbol": "MNQ", "conid": "1", "sec_type": "FUT",
             "expiry": "20200619"}) is True
        assert needs_qualification(
            {"symbol": "MNQ", "conid": "1", "sec_type": "FUT",
             "expiry": "29990619"}) is False

    def test_opt_always_skipped(self):
        assert needs_qualification(
            {"symbol": _OCC, "sec_type": "OPT"}) is False
        assert needs_qualification({"symbol": _OCC}) is False  # OCC shape


class TestGuessParams:
    def test_known_futures_root_browses_fut(self):
        p = guess_browse_params({"symbol": "MNQ"})
        assert p["sec_type"] == "FUT" and p["symbol"] == "MNQ"

    def test_unknown_symbol_defaults_to_smart_stk(self):
        p = guess_browse_params({"symbol": "SPCX"})
        assert p == {"sec_type": "STK", "symbol": "SPCX",
                     "exchange": "SMART", "currency": "USD"}

    def test_explicit_sec_type_wins(self):
        assert guess_browse_params(
            {"symbol": "ZZZ", "sec_type": "FUT"})["sec_type"] == "FUT"


class TestQueue:
    def _make(self, cfg, chips):
        _write(cfg, {"book_trader_symbols": chips})
        worker = _FakeWorker()
        q = InstrumentQualifier({"w": worker})
        q.scan_workers()
        return q, worker

    def test_qualify_upserts_and_signals(self, qtbot, cfg):
        q, worker = self._make(cfg, ["SPCX"])
        healed = []
        q.contract_requalified.connect(healed.append)
        q._build_batch()
        assert worker.browsed[0]["sec_type"] == "STK"
        worker.browse_results.emit([{
            "symbol": "SPCX", "sec_type": "STK", "conid": 602261424,
            "exchange": "SMART", "currency": "USD",
            "local_symbol": "SPCX", "primary_exchange": "ARCA"}])
        assert healed == ["SPCX"]
        assert instruments.get_contract("SPCX")["conid"] == "602261424"
        # Once per session: a second batch does nothing.
        q._build_batch()
        assert len(worker.browsed) == 1

    def test_expired_fut_repoints_to_front_month(self, qtbot, cfg):
        _write(cfg, {"book_trader_symbols": [
            {"symbol": "MNQ", "conid": "1", "sec_type": "FUT",
             "exchange": "CME", "currency": "USD", "expiry": "20200619",
             "local_symbol": "MNQM0"}]})
        worker = _FakeWorker()
        q = InstrumentQualifier({"w": worker})
        q.scan_workers()
        q._build_batch()
        assert worker.browsed[0]["sec_type"] == "FUT"
        worker.browse_results.emit([
            {"symbol": "MNQ", "sec_type": "FUT", "conid": 902,
             "exchange": "CME", "currency": "USD", "expiry": "29990918",
             "local_symbol": "MNQU9", "multiplier": "2", "min_tick": 0.25},
            {"symbol": "MNQ", "sec_type": "FUT", "conid": 901,
             "exchange": "CME", "currency": "USD", "expiry": "29990619",
             "local_symbol": "MNQM9", "multiplier": "2", "min_tick": 0.25},
        ])
        e = instruments.get_contract("MNQ")
        assert e["conid"] == "901"            # soonest unexpired
        assert e["local_symbol"] == "MNQM9"
        assert e["min_tick"] == 0.25

    def test_crosstalk_results_ignored(self, qtbot, cfg):
        q, worker = self._make(cfg, ["SPCX"])
        q._build_batch()
        worker.browse_results.emit([{"symbol": "OTHER", "sec_type": "STK",
                                     "conid": 1}])
        assert q._busy_symbol == "SPCX"       # still waiting for ours
        worker.browse_results.emit([{
            "symbol": "SPCX", "sec_type": "STK", "conid": 5,
            "currency": "USD"}])
        assert q._busy_symbol is None
        assert instruments.get_contract("SPCX")["conid"] == "5"

    def test_failure_releases_and_continues(self, qtbot, cfg):
        q, worker = self._make(cfg, ["SPCX", "BARE2"])
        q._build_batch()
        worker.browse_failed.emit("no match")
        # Second symbol pumped after the failure (queue order follows
        # all_contracts' symbol sort).
        assert sorted(p["symbol"] for p in worker.browsed) == \
            ["BARE2", "SPCX"]
        assert len(worker.browsed) == 2
