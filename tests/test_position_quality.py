from pathlib import Path

from sniper_paper.storage import Journal


def _position(journal: Journal, position_id: str, quantity: float, opened: int, closed: int, pnl: float = 1.0) -> None:
    signal_id = f"signal-{position_id}"
    journal.record_signal(
        {
            "signal_id": signal_id,
            "occurred_at_ms": opened,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "lane": "lane",
            "target_timeframe": "4h",
            "level_class": "cluster",
            "trigger_price": 100.0,
            "stop_price": 99.0,
            "target_price": 101.0,
            "status": "TRIGGERED",
            "reason": "fixture",
            "protocol_hash": "test",
            "features": {},
        }
    )
    journal.open_position(
        {
            "position_id": position_id,
            "signal_id": signal_id,
            "opened_at_ms": opened,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "lane": "lane",
            "entry_price": 100.0,
            "stop_price": 99.0,
            "target_price": 101.0,
            "quantity": quantity,
            "entry_fee": 0.0,
            "status": "OPEN",
        }
    )
    journal.close_position(
        position_id,
        {
            "closed_at_ms": closed,
            "exit_price": 101.0,
            "exit_fee": 0.0,
            "exit_reason": "TP",
            "gross_pnl": pnl,
            "net_pnl": pnl,
        },
    )


def test_quality_annotations_are_read_only_and_persist_across_journal_reopen(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    _position(journal, "dust", 5.329e-15, 1_000, 2_000, 0.0)
    _position(journal, "real", 1e-8, 3_000, 4_000, 1.0)
    journal.event(1_500, "WARN", "STREAM_DISCONNECTED", "gap")

    history = journal.position_history()
    dust = next(row for row in history if row["position_id"] == "dust")
    real = next(row for row in history if row["position_id"] == "real")
    assert dust["execution_quality_ok"] is False
    assert dust["quality_reasons"] == ["numerical_dust", "stream_disconnected"]
    assert real["execution_quality_ok"] is True
    assert journal.dashboard_snapshot()["lane_pnl"] == [{"lane": "lane", "trades": 1, "wins": 1, "losses": 0, "net_pnl": 1.0}]
    assert journal.realized_pnl() == 1.0

    reopened = Journal(database)
    assert reopened.position_history()[0]["quantity"] == 1e-8
    with reopened.connect() as db:
        assert db.execute("SELECT quantity FROM positions WHERE position_id='dust'").fetchone()[0] == 5.329e-15


def test_gap_and_restart_boundaries_are_inclusive(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    _position(journal, "before", 1.0, 100, 200)
    _position(journal, "at-open", 1.0, 300, 400)
    _position(journal, "at-close", 1.0, 500, 600)
    journal.event(300, "INFO", "START", "restart")
    journal.event(600, "WARN", "STREAM_DISCONNECTED", "gap")
    rows = {row["position_id"]: row for row in journal.position_history()}
    assert rows["before"]["execution_quality_ok"] is True
    assert rows["at-open"]["quality_reasons"] == ["service_restart"]
    assert rows["at-close"]["quality_reasons"] == ["stream_disconnected"]


def test_small_legitimate_crypto_quantity_is_not_dust(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    _position(journal, "small", 1e-10, 1_000, 2_000)
    row = journal.position_history()[0]
    assert row["execution_quality_ok"] is True
    assert row["quality_reasons"] == []


def test_gap_stats_are_separate_and_raw_accounting_is_preserved(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    _position(journal, "gap", 1.0, 100, 200, -2.0)
    _position(journal, "clear", 1.0, 300, 400, 3.0)
    _position(journal, "dust", 5e-15, 500, 600, -1e-14)
    journal.event(150, "WARN", "STREAM_DISCONNECTED", "gap")
    snapshot = journal.dashboard_snapshot()
    assert snapshot["lane_pnl"][0]["trades"] == 2
    assert snapshot["lane_pnl"][0]["net_pnl"] == 1.0
    assert snapshot["eligible_lane_pnl"][0]["trades"] == 1
    assert snapshot["eligible_lane_pnl"][0]["net_pnl"] == 3.0
    assert snapshot["raw_lane_pnl"][0]["trades"] == 3
    assert journal.realized_pnl() == 1.0 - 1e-14
    assert "evaluation_eligible" not in snapshot
