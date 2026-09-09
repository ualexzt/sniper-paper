import sqlite3
from pathlib import Path

from sniper_paper.storage import SCHEMA_VERSION, Journal


def level(**overrides):
    value = {
        "level_id": "btc-15m-high-1",
        "revision": 1,
        "version": "levels-v1",
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "side": "HIGH",
        "price": 100.0,
        "zone_low": 99.9,
        "zone_high": 100.1,
        "touches": 3,
        "level_class": "cluster",
        "provenance": {"source": "pivot", "members": ["p1", "p2"]},
        "origin_at_ms": 1_000,
        "confirmed_at_ms": 2_000,
        "first_seen_at_ms": 2_000,
        "broken_at_ms": None,
        "invalidation_reason": None,
        "updated_at_ms": 2_000,
    }
    value.update(overrides)
    return value


def test_level_catalog_migrates_additively_and_upsert_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    journal.set_meta("unrelated", "preserve-me")

    first = journal.upsert_level(level())
    second = Journal(database).upsert_level(level())

    assert first == second
    assert Journal(database).get_meta("unrelated") == "preserve-me"
    assert Journal(database).get_meta("schema_version") == str(SCHEMA_VERSION)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM levels").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM level_history").fetchone()[0] == 0


def test_break_state_survives_restart_and_stale_bootstrap(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    journal = Journal(database)
    journal.upsert_level(level())
    broken = journal.mark_level_broken("btc-15m-high-1", 3_000, "confirmed_close_break")
    assert broken["broken_at_ms"] == 3_000
    assert broken["invalidation_reason"] == "confirmed_close_break"

    # A fresh detector snapshot has no break yet; it must not resurrect the row.
    restored = Journal(database)
    current = restored.upsert_level(level(revision=2, price=100.05, broken_at_ms=None, updated_at_ms=4_000))
    assert current["broken_at_ms"] == 3_000
    assert current["invalidation_reason"] == "confirmed_close_break"
    assert restored.level_row("btc-15m-high-1")["broken_at_ms"] == 3_000
    history = restored.level_history_rows("btc-15m-high-1")
    assert len(history) == 2
    assert {item["broken_at_ms"] for item in history} == {None, 3_000}


def test_load_levels_returns_decoded_provenance_and_causal_filters(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    journal.upsert_level(level())
    journal.upsert_level(
        level(
            level_id="btc-4h-low-1",
            timeframe="4h",
            side="LOW",
            price=90.0,
            zone_low=89.9,
            zone_high=90.1,
            origin_at_ms=500,
            confirmed_at_ms=1_500,
            first_seen_at_ms=1_500,
            updated_at_ms=1_500,
        )
    )
    journal.mark_level_broken("btc-4h-low-1", 2_500, "wick_break")

    assert journal.load_levels(symbol="BTCUSDT", timeframe="15m")[0]["provenance"] == {
        "source": "pivot",
        "members": ["p1", "p2"],
    }
    assert len(journal.load_levels(include_broken=False)) == 1
    assert len(journal.load_levels(as_of_ms=2_000)) == 2
    assert len(journal.load_levels(as_of_ms=2_500)) == 1
    assert len(journal.load_levels(version="levels-v1")) == 2
    assert journal.load_levels(version="other-level-version") == []
    assert journal.load_levels(limit=0) == []
