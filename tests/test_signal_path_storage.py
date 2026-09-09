import sqlite3
from pathlib import Path

import pytest

from sniper_paper.storage import Journal, SCHEMA_VERSION


def event(**overrides):
    value = {
        "signal_id": "sig-diagnostic-1",
        "occurred_at_ms": 2_000,
        "event_type": "SETUP_OBSERVED",
        "price": 100.25,
        "reference_price": 100.0,
        "tp_touched": "UNKNOWN",
        "sl_touched": "UNKNOWN",
        "mfe_bp": None,
        "mae_bp": None,
        "coverage": 0.75,
        "status": "OBSERVED",
        "reason": "completed_bar",
        "features": {"level_id": "l1", "bars": 20},
        "provenance": {"source": "public_bybit", "version": "metrics-v1"},
    }
    value.update(overrides)
    return value


def test_signal_path_migration_is_additive_and_unknown_signal_is_allowed(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    journal.set_meta("keep", "yes")
    assert journal.record_signal_path_event(event(signal_id="candidate-without-execution"))

    restarted = Journal(database)
    assert restarted.get_meta("keep") == "yes"
    assert restarted.get_meta("schema_version") == str(SCHEMA_VERSION)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM signal_path_events").fetchone()[0] == 1


def test_signal_path_events_are_ordered_and_decode_features_provenance(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    journal.record_signal_path_event(event(occurred_at_ms=3_000, event_type="TP_TOUCH", tp_touched="YES", status="CLOSED"))
    journal.record_signal_path_event(event(occurred_at_ms=1_000, event_type="SETUP_OBSERVED"))
    journal.record_signal_path_event(
        event(
            occurred_at_ms=2_000,
            event_type="OUTCOME_UNCERTAIN",
            tp_touched="AMBIGUOUS",
            sl_touched="AMBIGUOUS",
            status="AMBIGUOUS",
            reason="same_bar_tp_sl_order_unknown",
            coverage=None,
            mfe_bp=12.5,
            mae_bp=-8.0,
        )
    )
    rows = journal.signal_path_events("sig-diagnostic-1")
    assert [row["event_type"] for row in rows] == ["SETUP_OBSERVED", "OUTCOME_UNCERTAIN", "TP_TOUCH"]
    assert rows[1]["tp_touched"] == rows[1]["sl_touched"] == "AMBIGUOUS"
    assert rows[1]["coverage"] is None
    assert rows[1]["mfe_bp"] == pytest.approx(12.5)
    assert rows[0]["features"]["level_id"] == "l1"
    assert rows[0]["provenance"]["source"] == "public_bybit"


def test_signal_path_identity_is_idempotent_and_first_observation_wins(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    first = event(occurred_at_ms=4_000, event_type="PRICE_SAMPLE", price=100.0)
    duplicate = event(occurred_at_ms=4_000, event_type="PRICE_SAMPLE", price=101.0, status="UNKNOWN")
    assert journal.record_signal_path_event(first)
    assert not journal.record_signal_path_event(duplicate)
    assert journal.signal_path_events()[0]["price"] == 100.0

    explicit = event(event_id="explicit-1", event_type="PRICE_SAMPLE", occurred_at_ms=5_000)
    assert journal.record_signal_path_event(explicit)
    assert not journal.record_signal_path_event({**explicit, "price": 999.0})


def test_signal_path_restart_and_invalid_unknown_representation(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    journal.record_signal_path_event(event(event_type="MFE_UPDATE", mfe_bp=10.0, mae_bp=-4.0))
    restored = Journal(database)
    row = restored.signal_path_events("sig-diagnostic-1")[0]
    assert row["mfe_bp"] == 10.0
    assert row["mae_bp"] == -4.0
    assert restored.signal_path_event(row["event_id"]) == row

    with pytest.raises(ValueError, match="touch state"):
        restored.record_signal_path_event(event(event_id="bad", tp_touched="MAYBE"))


def test_signal_path_events_can_be_filtered_by_signal_symbol(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    journal.record_signal(
        {
            "signal_id": "sig-diagnostic-1",
            "occurred_at_ms": 1_000,
            "symbol": "XUSDT",
            "side": "LONG",
            "lane": "lane-a",
            "target_timeframe": "15m",
            "level_class": "cluster",
            "trigger_price": 100.0,
            "stop_price": 99.0,
            "target_price": 102.0,
            "status": "TRIGGERED",
            "reason": "test",
            "protocol_hash": "hash",
        }
    )
    journal.record_signal_path_event(event())

    rows = journal.signal_path_events_for_symbol("XUSDT")

    assert len(rows) == 1
    assert rows[0]["symbol"] == "XUSDT"
    assert rows[0]["lane"] == "lane-a"
    assert journal.signal_path_events_for_symbol("OTHERUSDT") == []
