from pathlib import Path

from sniper_paper.dashboard_adapter import journal_dashboard
from sniper_paper.storage import Journal


def test_dashboard_surfaces_observation_mode_and_book_readiness(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    journal.record_universe(
        run_id="u1",
        selected_at_ms=1,
        utc_date="2026-09-07",
        protocol_hash="abc",
        source={"evaluation_eligible": False},
        members=[
            {
                "symbol": "BTCUSDT",
                "rank": 1,
                "selected": True,
                "metrics": {"spread_bps": "1.25", "depth_notional_top5": "10000"},
            }
        ],
    )
    import time

    journal.set_meta("stream_state", "connected")
    journal.event(time.time_ns() // 1_000_000, "INFO", "HEARTBEAT", "paper service healthy; books 1/1")
    result = journal_dashboard(journal)
    assert result["headline"].startswith("Observation only")
    assert result["data_health"][0]["status"] == "Connected"
    assert result["data_health"][1]["status"] == "Ready"
    assert result["current_universe"][0]["spread_bp"] == "1.25 bp"
    assert "daily rank 1" in result["current_universe"][0]["reason"]
    assert "NATR unavailable" in result["current_universe"][0]["reason"]
    assert result["current_universe"][0]["timeframe"] == "levels 1m / 5m / 15m / 30m / 1h / 4h / 1d"
