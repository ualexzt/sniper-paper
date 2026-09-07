import sqlite3
from pathlib import Path

import pytest

from sniper_paper.paper import PaperExecutor, PaperSignal, Quote, Side
from sniper_paper.storage import Journal


def signal_row(signal: PaperSignal) -> dict:
    return {
        "signal_id": signal.signal_id,
        "occurred_at_ms": signal.occurred_at_ms,
        "symbol": signal.symbol,
        "side": signal.side.value,
        "lane": signal.lane,
        "target_timeframe": "4h",
        "level_class": "cluster",
        "trigger_price": 100.0,
        "stop_price": signal.stop_price,
        "target_price": signal.target_price,
        "status": "TRIGGERED",
        "reason": "fixture",
        "protocol_hash": "abc",
        "features": {"delta": 1.0},
    }


def test_long_opens_after_latency_and_closes_at_tp(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "early_target_hunt", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, slippage_bp=0, latency_ms=250)
    assert executor.submit(signal)
    assert executor.on_quote("BTCUSDT", Quote(1_249, 100, 100.1)) is None
    opened = executor.on_quote("BTCUSDT", Quote(1_250, 100, 100.1))
    assert opened and opened["event"] == "OPEN"
    assert executor.on_quote("BTCUSDT", Quote(1_300, 103, 103.1))["exit_reason"] == "TP"
    snapshot = journal.dashboard_snapshot()
    assert snapshot["positions"] == []
    assert snapshot["lane_pnl"][0]["trades"] == 1
    assert snapshot["lane_pnl"][0]["net_pnl"] > 0


def test_short_stop_and_single_position_gate(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "ETHUSDT", Side.SHORT, "terminal_level_breakout", 102, 96)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, slippage_bp=0, latency_ms=0)
    assert executor.submit(signal)
    assert not executor.submit(signal)
    executor.on_quote("ETHUSDT", Quote(1, 99.9, 100))
    result = executor.on_quote("ETHUSDT", Quote(2, 102, 102.1))
    assert result and result["exit_reason"] == "SL"
    assert result["net_pnl"] < 0


def test_duplicate_signal_and_double_close_are_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "BTCUSDT", Side.LONG, "early_target_hunt", 99, 102)
    journal.record_signal(signal_row(signal))
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_signal(signal_row(signal))


def test_universe_snapshot_is_immutable_per_utc_day(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    kwargs = {
        "run_id": "r1",
        "selected_at_ms": 1,
        "utc_date": "2026-09-07",
        "protocol_hash": "abc",
        "source": {"endpoint": "public"},
        "members": [{"symbol": "BTCUSDT", "rank": 1, "selected": True}],
    }
    journal.record_universe(**kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_universe(**{**kwargs, "run_id": "r2"})
    run = journal.universe_run_for_date("2026-09-07")
    assert run is not None
    assert run["source"] == {"endpoint": "public"}


def test_open_position_restores_after_restart(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "BTCUSDT", Side.LONG, "early_target_hunt", 99, 102)
    journal.record_signal(signal_row(signal))
    first = PaperExecutor(journal, slippage_bp=0, latency_ms=0)
    first.submit(signal)
    first.on_quote("BTCUSDT", Quote(1, 100, 100.1))
    restored = PaperExecutor(journal, slippage_bp=0, latency_ms=0)
    assert restored.restore()
    assert restored.position is not None
    assert restored.position.signal.signal_id == "s1"
    assert restored.on_quote("BTCUSDT", Quote(2, 102, 102.1))["exit_reason"] == "TP"


def test_restart_marks_memory_only_trigger_as_missed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "BTCUSDT", Side.LONG, "early_target_hunt", 99, 102)
    journal.record_signal(signal_row(signal))
    assert journal.reconcile_orphaned_triggers() == 1
    assert journal.signal_row("s1")["status"] == "MISSED"
