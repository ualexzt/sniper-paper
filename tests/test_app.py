import asyncio
from pathlib import Path

import pytest

from sniper_paper.app import PaperApp, SymbolState, _parse_klines
from sniper_paper.market import Bar, Trade
from sniper_paper.orderflow import Footprint
from sniper_paper.storage import Journal
from sniper_paper.strategy import Level, LevelSide


def test_parse_klines_orders_oldest_first_and_excludes_forming() -> None:
    rows = [
        [120_000, "3", "4", "2", "3.5", "5", "17"],
        [60_000, "2", "3", "1", "2.5", "4", "10"],
        [0, "1", "2", "0.5", "1.5", "3", "4"],
    ]
    bars = _parse_klines("XUSDT", "1m", rows, 150_000)
    assert [item.opened_at_ms for item in bars] == [0, 60_000]
    assert bars[-1].close == 2.5


def test_market_detail_exposes_real_state_and_rejects_out_of_universe(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.bars["5m"] = [Bar("XUSDT", 300_000, 0, 300_000, 100, 102, 99, 101, 10, 1, 2)]
    state.levels = [Level("l1", "XUSDT", "4h", LevelSide.HIGH, 110, 1)]
    state.book.apply(
        {
            "type": "snapshot",
            "data": {
                "s": "XUSDT",
                "u": 1,
                "seq": 1,
                "b": [["100", "2"]],
                "a": [["102", "3"]],
            },
        },
        1,
    )
    app.states = {"XUSDT": state}
    result = app.market_detail("XUSDT", "5m")
    assert result["bars"][0]["close"] == 101
    assert result["levels"][0]["level_id"] == "l1"
    assert result["quote"] == {"bid": 100.0, "ask": 102.0, "mid": 101.0}
    with pytest.raises(ValueError, match="daily universe"):
        app.market_detail("OTHERUSDT", "5m")


def test_level_break_is_absorbed_into_state_and_hidden_from_dashboard(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT", tick_size=0.01)
    state.levels = [Level("l1", "XUSDT", "4h", LevelSide.HIGH, 100.0, 0)]
    state.bars["1m"] = [
        Bar("XUSDT", 60_000, 0, 60_000, 99, 101, 98, 100.01, 1, 0, 1),
        Bar("XUSDT", 60_000, 60_000, 120_000, 100, 101, 99, 100.02, 1, 0, 1),
    ]
    state.bars["5m"] = [Bar("XUSDT", 300_000, 0, 300_000, 99, 101, 98, 100, 1, 0, 1)]
    app.states = {"XUSDT": state}

    app._refresh_level_lifecycle(state, 120_000)

    assert state.levels[0].broken_at_ms == 120_000
    assert app.market_detail("XUSDT", "5m")["levels"] == []


def test_market_detail_appends_forming_bar_for_dashboard_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.bars["1m"] = [Bar("XUSDT", 60_000, 0, 60_000, 99, 101, 98, 100, 10, 1, 2)]
    state.builders["1m"].add(Trade("XUSDT", 61_000, "live", 102, 3, "Buy"))
    app.states = {"XUSDT": state}
    monkeypatch.setattr("sniper_paper.app._now_ms", lambda: 65_000)

    result = app.market_detail("XUSDT", "1m")

    assert len(result["bars"]) == 2
    assert result["bars"][0]["is_forming"] is False
    assert result["bars"][1]["is_forming"] is True
    assert result["bars"][1]["close"] == 102
    assert result["bar_closes_at_ms"] == 120_000
    assert result["server_time_ms"] == 65_000
    assert len(state.bars["1m"]) == 1


def test_market_detail_readiness_requires_every_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.book.apply(
        {
            "type": "snapshot",
            "data": {"s": "XUSDT", "u": 1, "seq": 1, "b": [["100", "2"]], "a": [["100.01", "3"]]},
        },
        1_000_000,
    )
    state.snapshot_received_at_ms = 600_000
    state.orderflow_ready = True
    app.states = {"XUSDT": state}
    app.stream_connected = True
    app.evaluation_eligible = True
    monkeypatch.setattr("sniper_paper.app._now_ms", lambda: 1_000_000)

    ready = app.market_detail("XUSDT", "5m")["readiness"]
    assert ready["ready"] is True
    assert ready["blocker"] is None

    state.orderflow_ready = False
    blocked = app.market_detail("XUSDT", "5m")["readiness"]
    assert blocked["ready"] is False
    assert blocked["blocker"] == "orderflow_incomplete"


def test_orderbook_message_does_not_drop_completed_footprint(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT", tick_size=0.01)
    app.states = {"XUSDT": state}
    app.connection_id = "c1"
    app.footprint = Footprint({"XUSDT": "0.01"}, warmup_ms=1)

    asyncio.run(
        app.handle_message(
            {
                "topic": "publicTrade.XUSDT",
                "data": [{"s": "XUSDT", "i": "t1", "p": "100.00", "v": "1", "S": "Buy"}],
            },
            1_000,
        )
    )
    asyncio.run(
        app.handle_message(
            {
                "topic": "orderbook.50.XUSDT",
                "type": "snapshot",
                "data": {"s": "XUSDT", "u": 1, "seq": 1, "b": [["99.99", "2"]], "a": [["100.01", "3"]]},
            },
            15_001,
        )
    )

    assert len(state.bars["15s"]) == 1
    assert state.bars["15s"][0].close == 100.0


def test_observation_only_still_refreshes_orderflow_panel(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.book.apply(
        {
            "type": "snapshot",
            "data": {"s": "XUSDT", "u": 1, "seq": 1, "b": [["100", "2"]], "a": [["100.01", "3"]]},
        },
        15_000,
    )
    state.snapshot_received_at_ms = 0
    state.orderflow_ready = True
    state.bars["15s"] = [Bar("XUSDT", 15_000, 0, 15_000, 100, 100.01, 99.99, 100, 1, 250, 2)]
    app.stream_connected = True
    app.evaluation_eligible = False

    app._evaluate_v2(state, {"stacks": [{"side": "buy"}]}, 15_000)

    assert state.last_orderflow is not None
    assert state.last_orderflow["delta_15s"] == 250
    assert state.last_orderflow["footprint_stack"] == 1
    assert "status" not in state.last_orderflow
