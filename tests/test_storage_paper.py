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


def quote(ts: int, bid: float = 100.0, ask: float = 100.1, bid_size: float = 5.0, ask_size: float = 6.0) -> Quote:
    return Quote(ts, bid, ask, bid_size, ask_size, {bid: bid_size}, {ask: ask_size})


def test_strict_queue_entry_then_tp(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "failed_sweep_reclaim", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, slippage_bp=0, latency_ms=250, maker_fee_rate=0)
    assert executor.submit(signal, quote(1_000))
    assert executor.on_quote("BTCUSDT", quote(1_249)) is None
    assert executor.on_quote("BTCUSDT", quote(1_250))["event"] == "ACTIVE"
    queued = executor.on_trade("BTCUSDT", 1_300, "Sell", 100.0, 5.0)
    assert queued == {"event": "QUEUE", "queue_consumed": 5.0, "filled_qty": 0.0}
    opened = executor.on_trade("BTCUSDT", 1_301, "Sell", 100.0, 20.0)
    assert opened and opened["event"] == "OPEN"
    closed = executor.on_quote("BTCUSDT", quote(1_400, 103, 103.1))
    assert closed and closed["exit_reason"] == "TP"
    assert journal.dashboard_snapshot()["lane_pnl"][0]["net_pnl"] > 0


def test_cancellations_do_not_improve_queue_and_partial_fill_restores(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "failed_sweep_reclaim", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, slippage_bp=0, latency_ms=0, maker_fee_rate=0)
    assert executor.submit(signal, quote(1_000, bid_size=5))
    executor.on_quote("BTCUSDT", quote(1_000, bid_size=5))
    executor.on_quote("BTCUSDT", quote(1_050, bid_size=2))
    assert executor.pending and executor.pending.queue_ahead_qty == 5
    executor.on_trade("BTCUSDT", 1_100, "Sell", 100, 6)
    assert executor.position and executor.position.quantity == pytest.approx(1)
    assert executor.pending and executor.pending.status == "PARTIAL"

    restored = PaperExecutor(journal, slippage_bp=0, latency_ms=0, maker_fee_rate=0)
    assert restored.restore()
    assert restored.position and restored.position.quantity == pytest.approx(1)
    assert restored.pending and restored.pending.filled_qty == pytest.approx(1)
    restored.on_trade("BTCUSDT", 1_200, "Sell", 100, 100)
    assert restored.pending is None


def test_no_touch_expires_as_missed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "failed_sweep_reclaim", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=100, entry_ttl_ms=2_000)
    assert executor.submit(signal, quote(1_000))
    executor.on_quote("BTCUSDT", quote(1_100))
    result = executor.on_quote("BTCUSDT", quote(3_100))
    assert result and result["event"] == "MISSED"
    assert executor.position is None and executor.pending is None
    assert journal.signal_row("s1")["status"] == "MISSED"


def test_wrong_trade_side_or_price_never_fills(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "ETHUSDT", Side.SHORT, "terminal_level_breakout", 102, 96)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=0)
    assert executor.submit(signal, quote(0))
    executor.on_quote("ETHUSDT", quote(0))
    assert executor.on_trade("ETHUSDT", 1, "Sell", 100.1, 100) is None
    assert executor.on_trade("ETHUSDT", 2, "Buy", 100.0, 100) is None
    assert executor.position is None


def test_float_queue_residue_is_not_a_position(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("dust", 0, "BTCUSDT", Side.LONG, "cascade_impulse", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=0)
    assert executor.submit(signal, quote(0, bid_size=0.3), qty_step=0.1)
    executor.on_quote("BTCUSDT", quote(0, bid_size=0.3))
    result = executor.on_trade("BTCUSDT", 1, "Sell", 100.0, 0.1 + 0.2)
    assert result and result["event"] == "QUEUE" and result["filled_qty"] == 0.0
    assert executor.position is None


def test_small_real_partial_fill_is_retained(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("partial", 0, "BTCUSDT", Side.LONG, "cascade_impulse", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=0)
    assert executor.submit(signal, quote(0, bid_size=0.3), qty_step=0.1)
    executor.on_quote("BTCUSDT", quote(0, bid_size=0.3))
    result = executor.on_trade("BTCUSDT", 1, "Sell", 100.0, 0.4)
    assert result and result["event"] == "PARTIAL"
    assert executor.position is not None and executor.position.quantity == pytest.approx(0.1)


def test_small_partial_survives_large_order_scale_and_restore(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    signal = PaperSignal("large", 0, "BTCUSDT", Side.LONG, "cascade_impulse", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=0)
    assert executor.submit(signal, quote(0, bid_size=0.3), qty_step=0.00001)
    assert executor.pending is not None
    executor.pending.quantity = 1e12
    journal.update_paper_order(executor.pending.order_id, quantity=1e12)
    executor.on_quote("BTCUSDT", quote(0, bid_size=0.3))
    result = executor.on_trade("BTCUSDT", 1, "Sell", 100.0, 0.30001)
    assert result and result["event"] == "PARTIAL"
    assert executor.position is not None and executor.position.quantity == pytest.approx(0.00001)
    restored = PaperExecutor(journal, latency_ms=0)
    assert restored.restore()
    assert restored.position is not None and restored.position.quantity == pytest.approx(0.00001)


def test_duplicate_signal_and_universe_day_are_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 0, "BTCUSDT", Side.LONG, "early_target_hunt", 99, 102)
    journal.record_signal(signal_row(signal))
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_signal(signal_row(signal))
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


def test_partial_ttl_keeps_real_position_and_cancels_remainder(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "failed_sweep_reclaim", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=0, entry_ttl_ms=100)
    executor.submit(signal, quote(1_000, bid_size=1))
    executor.on_quote("BTCUSDT", quote(1_000, bid_size=1))
    executor.on_trade("BTCUSDT", 1_050, "Sell", 100, 2)
    assert executor.position is not None
    result = executor.on_quote("BTCUSDT", quote(1_100))
    assert result and result["event"] == "PARTIAL_EXPIRED"
    assert executor.position is not None and executor.pending is None
    assert journal.signal_row("s1")["status"] == "OPEN"


def test_restart_reconcile_preserves_persisted_pending_order(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    signal = PaperSignal("s1", 1_000, "BTCUSDT", Side.LONG, "failed_sweep_reclaim", 99, 103)
    journal.record_signal(signal_row(signal))
    executor = PaperExecutor(journal, latency_ms=250)
    assert executor.submit(signal, quote(1_000))

    assert journal.reconcile_orphaned_triggers() == 0
    restored = PaperExecutor(journal, latency_ms=250)
    assert restored.restore()
    assert restored.pending and restored.pending.signal.signal_id == "s1"
    assert journal.signal_row("s1")["status"] == "TRIGGERED"


def test_shadow_diagnostic_is_idempotent_and_has_no_execution_link(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    row = {
        "diagnostic_id": "diag-1",
        "setup_id": "setup-1",
        "occurred_at_ms": 1_000,
        "symbol": "BTCUSDT",
        "lane": "retest_reclaim_v1",
        "side": "LONG",
        "status": "OBSERVED",
        "reason": "causal_retest_confirmed",
        "reference_price": 100.0,
        "stop_price": 99.5,
        "target_price": 102.0,
        "protocol_hash": "shadow-hash",
        "features": {"level_id": "broken-1"},
    }

    assert journal.record_shadow_diagnostic(row) is True
    assert journal.record_shadow_diagnostic(row) is False
    assert Journal(database).record_shadow_diagnostic(row) is False
    assert journal.shadow_diagnostics("BTCUSDT") == [{**row, "features": {"level_id": "broken-1"}}]
    assert journal.shadow_diagnostics("BTCUSDT", protocol_hash="other") == []

    with journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
