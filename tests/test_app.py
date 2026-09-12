import asyncio
from pathlib import Path

import pytest

from sniper_paper.app import (
    BAR_RETENTION,
    DIGASH_LEVEL_VERSION,
    HISTORY_LIMIT,
    PaperApp,
    SymbolState,
    _history_diagnostics,
    _parse_klines,
)
from sniper_paper.market import Bar, Trade
from sniper_paper.orderflow import Footprint
from sniper_paper.paper import PaperSignal, Side
from sniper_paper.storage import Journal
from sniper_paper.strategy import Level, LevelSide
from sniper_paper.strategy_v2 import DecisionStatus, StrategyDecision, StrategySignal, StrategyV2Evaluator


@pytest.mark.parametrize("book_first", [True, False])
def test_boundary_footprint_evaluated_once_after_trade_execution(tmp_path, monkeypatch, book_first):
    from types import SimpleNamespace
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    app.states = {state.symbol: state}
    state.builders["1m"].add(Trade("XUSDT", 1_000, "seed", 100, 1, "Buy"))
    footprint = {"symbol": "XUSDT", "bucket_start_ms": 45_000, "bucket_end_ms": 60_000,
                 "open": 100, "high": 101, "low": 100, "close": 101, "volume": 1,
                 "delta_notional": 100, "trades": 1}
    events = []
    monkeypatch.setattr(app, "_refresh_digash_levels", lambda *args: None)
    monkeypatch.setattr(app, "_refresh_level_lifecycle", lambda *args: None)
    monkeypatch.setattr(app.executor, "on_trade", lambda *args: events.append("trade"))
    def evaluate(s, fp, now):
        assert s.bars["1m"][-1].closed_at_ms == 60_000
        assert s.bars["15s"][-1].closed_at_ms == 60_000
        events.append("evaluate")
    monkeypatch.setattr(app, "_evaluate_v2", evaluate)
    if book_first:
        app._complete_footprint(footprint, 60_001)
        assert events == []
    app.footprint = SimpleNamespace(process=lambda row: [] if book_first else [footprint])
    message = {"topic": "publicTrade.XUSDT", "data": [
        {"s": "XUSDT", "i": "new", "p": "101", "v": "1", "S": "Buy"}]}
    asyncio.run(app.handle_message(message, 60_002))
    assert events == ["trade", "evaluate"]
    assert state.boundary_footprint is None


def test_parse_klines_orders_oldest_first_and_excludes_forming() -> None:
    rows = [
        [120_000, "3", "4", "2", "3.5", "5", "17"],
        [60_000, "2", "3", "1", "2.5", "4", "10"],
        [0, "1", "2", "0.5", "1.5", "3", "4"],
    ]
    bars = _parse_klines("XUSDT", "1m", rows, 150_000)
    assert [item.opened_at_ms for item in bars] == [0, 60_000]
    assert bars[-1].close == 2.5


def test_record_decision_rejects_bad_bracket_without_path_or_stream_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = Journal(tmp_path / "paper.db")
    app = PaperApp(journal)
    state = SymbolState("ENAUSDT")
    state.book.apply({"type": "snapshot", "data": {"s": "ENAUSDT", "u": 1, "seq": 1, "b": [["0.15060", "10"]], "a": [["0.15062", "10"]]}}, 1)
    bad = StrategySignal(
        "bad", 1, "ENAUSDT", Side.SHORT, "cascade_impulse", 0.15061527, 0.14622, 2_001, 60_001
    )
    decision = StrategyDecision(bad, "cascade_impulse", DecisionStatus.TRIGGERED.value, "frozen_rules_met", Side.SHORT, None, bad.target_price, bad.stop_price, bad.stop_price, "setup", {})
    monkeypatch.setattr(app.executor, "submit", lambda *args, **kwargs: pytest.fail("invalid signal submitted"))
    app._record_decision(state, decision, 0.15061, 1)
    assert app.signal_paths == {}
    assert journal.signal_row("bad")["status"] == "REJECTED"
    assert journal.signal_row("bad")["reason"] == "invalid_signal_bracket"

    monkeypatch.setattr(app.executor, "submit", lambda *args, **kwargs: False)
    good = StrategySignal(
        "good", 2, "ENAUSDT", Side.SHORT, "cascade_impulse", 0.151, 0.14622, 2_002, 60_002
    )
    good_decision = StrategyDecision(
        good,
        "cascade_impulse",
        DecisionStatus.TRIGGERED.value,
        "frozen_rules_met",
        Side.SHORT,
        None,
        good.target_price,
        good.stop_price,
        good.stop_price,
        "setup-good",
        {},
    )
    app._record_decision(state, good_decision, 0.15061, 2)
    assert "good" in app.signal_paths
    assert journal.signal_row("good")["status"] == "MISSED"


def test_history_diagnostics_distinguish_internal_gap_from_short_contiguous_history() -> None:
    contiguous = [Bar("XUSDT", 60_000, 0, 60_000, 1, 2, 0.5, 1.5, 1, 0, 1)]
    short = _history_diagnostics("1m", contiguous, requested_bars=HISTORY_LIMIT)
    assert short["coverage_status"] == "incomplete_history"
    assert short["short_contiguous_history"] is True
    assert short["interior_gap_count"] == 0

    gapped = [
        contiguous[0],
        Bar("XUSDT", 60_000, 120_000, 180_000, 1, 2, 0.5, 1.5, 1, 0, 1),
    ]
    diagnostics = _history_diagnostics("1m", gapped, requested_bars=2)
    assert diagnostics["coverage_status"] == "gapped"
    assert diagnostics["interior_gap_count"] == 1
    assert diagnostics["interior_missing_bars"] == 1
    assert diagnostics["short_contiguous_history"] is False


def test_bootstrap_requests_and_retains_1000_completed_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = 100_000_000_000

    class KlineClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_kline(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            interval = str(kwargs["interval"])
            milliseconds = {
                "1": 60_000,
                "5": 300_000,
                "15": 900_000,
                "30": 1_800_000,
                "60": 3_600_000,
                "240": 14_400_000,
                "D": 86_400_000,
            }[interval]
            rows = [
                [
                    str(i * milliseconds),
                    "100",
                    "101",
                    "99",
                    "100",
                    "1",
                    "1",
                ]
                for i in reversed(range(HISTORY_LIMIT))
            ]
            return {"result": {"list": rows}}

    monkeypatch.setattr("sniper_paper.app._now_ms", lambda: now_ms)
    client = KlineClient()
    app = PaperApp(Journal(tmp_path / "paper.db"), client=client)
    state = SymbolState("XUSDT")

    asyncio.run(app._bootstrap(state))

    assert len(client.calls) == 7
    assert {call["limit"] for call in client.calls} == {HISTORY_LIMIT}
    assert all(
        call["end"] == now_ms - (now_ms % {
            "1": 60_000, "5": 300_000, "15": 900_000, "30": 1_800_000,
            "60": 3_600_000, "240": 14_400_000, "D": 86_400_000,
        }[str(call["interval"])]) - 1
        for call in client.calls
    )
    level_timeframes = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
    assert all(len(state.bars[name]) == BAR_RETENTION for name in level_timeframes)
    assert all(
        state.history_diagnostics[name]["coverage_status"] == "complete"
        for name in level_timeframes
    )
    # Actual bootstrap must suppress partial startup buckets on every source TF.
    for name in ("4h", "1d"):
        builder = state.builders[name]
        boundary = (now_ms // builder.timeframe_ms + 1) * builder.timeframe_ms
        builder.add(Trade("XUSDT", now_ms, "startup", 100, 1, "Buy"))
        assert builder.add(Trade("XUSDT", boundary, "boundary", 101, 1, "Buy")) == []


def test_digash_refresh_drops_aged_active_levels_but_retains_broken_history(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.levels = [
        Level(
            "aged-active",
            "XUSDT",
            "1m",
            LevelSide.HIGH,
            110,
            1,
            level_class="digash_extreme",
            level_version=DIGASH_LEVEL_VERSION,
        ),
        Level(
            "broken",
            "XUSDT",
            "1h",
            LevelSide.LOW,
            90,
            1,
            level_class="digash_extreme",
            level_version=DIGASH_LEVEL_VERSION,
            broken_at_ms=2,
        ),
    ]

    app._refresh_digash_levels(state, 10)

    assert [level.level_id for level in state.levels] == ["broken"]


def test_digash_refresh_then_lifecycle_reconstructs_and_preserves_historical_break(
    tmp_path: Path,
) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT", tick_size=0.01)
    rows = [
        Bar(
            "XUSDT",
            60_000,
            index * 60_000,
            (index + 1) * 60_000,
            100.0,
            110.0 if index == 40 else (110.02 if index == 65 else (120.0 if index == 70 else 101.0)),
            99.0,
            110.02 if index == 65 else 100.0,
            1.0,
            0.0,
            1,
        )
        for index in range(120)
    ]
    state.bars["1m"] = rows
    now_ms = rows[-1].closed_at_ms

    app._refresh_digash_levels(state, now_ms)
    app._refresh_level_lifecycle(state, now_ms)
    broken = next(level for level in state.levels if level.origin_at_ms == rows[40].opened_at_ms)
    assert broken.broken_at_ms == rows[65].closed_at_ms

    app._refresh_digash_levels(state, now_ms)
    assert next(level for level in state.levels if level.level_id == broken.level_id).broken_at_ms == broken.broken_at_ms


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


def test_market_detail_exposes_shadow_metrics_and_quote_sized_liquidity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.bars["5m"] = [
        Bar(
            "XUSDT",
            300_000,
            index * 300_000,
            (index + 1) * 300_000,
            100 + index / 100,
            102 + index / 100,
            99 + index / 100,
            101 + index / 100,
            10 + index,
            0,
            1,
        )
        for index in range(289)
    ]
    now_ms = state.bars["5m"][-1].closed_at_ms
    state.book.apply(
        {
            "type": "snapshot",
            "data": {
                "s": "XUSDT",
                "u": 1,
                "seq": 1,
                "b": [["100", "1000"], ["99", "1000"]],
                "a": [["101", "1000"], ["102", "1000"]],
            },
        },
        now_ms,
    )
    app.states = {"XUSDT": state}
    monkeypatch.setattr("sniper_paper.app._now_ms", lambda: now_ms)

    result = app.market_detail("XUSDT", "5m")

    assert result["metrics"]["mode"] == "shadow_observation_only"
    assert result["metrics"]["natr_5m_14_pct"]["available"] is True
    assert result["metrics"]["dollar_volume_24h_pq"]["samples"] == 288
    observation = result["liquidity"]["observations"]["author_reference_50000_usd"]
    assert observation["buy"]["complete_depth"] is True
    assert observation["buy"]["vwap_price"] != observation["buy"]["marginal_impact_bps"]
    assert result["versions"] == app.version_info


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


def test_level_break_survives_restart_and_detector_replay(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    first = PaperApp(Journal(database))
    state = SymbolState("XUSDT", tick_size=0.01)
    level = Level("durable", "XUSDT", "15m", LevelSide.HIGH, 100.0, 0)
    first._merge_detected_levels(state, [level], 1)
    assert first.journal.level_row("durable")["first_seen_at_ms"] == 1
    state.bars["1m"] = [
        Bar("XUSDT", 60_000, 0, 60_000, 99, 101, 98, 100.01, 1, 0, 1),
        Bar("XUSDT", 60_000, 60_000, 120_000, 100, 101, 99, 100.02, 1, 0, 1),
    ]
    first._refresh_level_lifecycle(state, 120_000)
    assert state.levels[0].broken_at_ms == 120_000

    restarted = PaperApp(Journal(database))
    restored_state = SymbolState("XUSDT", tick_size=0.01)
    restored_state.levels = [
        restarted._level_from_storage(row)
        for row in restarted.journal.load_levels(symbol="XUSDT")
    ]
    restarted._merge_detected_levels(restored_state, [level], 180_000)

    assert len(restored_state.levels) == 1
    assert restored_state.levels[0].broken_at_ms == 120_000
    assert restarted.journal.level_row("durable")["broken_at_ms"] == 120_000
    assert len(restarted.journal.level_history_rows("durable")) == 1


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


def test_disconnect_quarantines_first_live_daily_bucket(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    app.states = {"XUSDT": state}

    # A stream observed from mid-day must not later be presented as a full UTC day.
    asyncio.run(app._trade({"s": "XUSDT", "i": "before", "p": "100", "v": "1", "S": "Buy"}, 15 * 3_600_000))
    asyncio.run(app.handle_message({"op": "connection", "state": "disconnected"}, 16 * 3_600_000))
    asyncio.run(app._trade({"s": "XUSDT", "i": "after", "p": "101", "v": "1", "S": "Buy"}, 86_400_000))

    assert state.bars["1d"] == []


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
    app.strategies = {"XUSDT": StrategyV2Evaluator()}

    app._evaluate_v2(state, {"stacks": [{"side": "buy"}]}, 15_000)

    assert state.last_orderflow is not None
    assert state.last_orderflow["delta_15s"] == 250
    assert state.last_orderflow["footprint_stack"] == 1
    assert "status" not in state.last_orderflow


def test_observation_only_records_classified_reaction_without_execution(tmp_path: Path, monkeypatch) -> None:
    journal = Journal(tmp_path / "paper.db")
    app = PaperApp(journal)
    state = SymbolState("XUSDT")
    now_ms = 400_000
    state.book.apply(
        {"type": "snapshot", "data": {"s": "XUSDT", "u": 1, "seq": 1,
                                        "b": [["100", "20"]], "a": [["100.01", "5"]]}},
        now_ms,
    )
    state.snapshot_received_at_ms = 0
    state.orderflow_ready = True
    state.bars["15s"] = [
        Bar("XUSDT", 15_000, i * 15_000, (i + 1) * 15_000, 100, 100.1, 99.9, 100, 1, 100, 2)
        for i in range(21)
    ]
    app.stream_connected = True
    app.evaluation_eligible = False
    signal = StrategySignal("observed", now_ms, "XUSDT", Side.LONG,
                            "terminal_level_breakout", 99.8, 100.4, now_ms + 2_000, now_ms + 60_000)
    level = Level("level", "XUSDT", "1m", LevelSide.HIGH, 99.9, 0)
    observed = StrategyDecision(signal, signal.lane, DecisionStatus.TRIGGERED.value, "frozen_rules_met",
                                Side.LONG, level, signal.target_price, signal.stop_price, level.price,
                                "setup", {"reaction_type": "orderflow_breakout"})

    class StubEvaluator:
        def evaluate(self, **kwargs):
            return [observed]

        def active_approaches(self):
            return []

    app.strategies = {"XUSDT": StubEvaluator()}
    monkeypatch.setattr(app.executor, "submit", lambda *args, **kwargs: pytest.fail("observation-only submitted"))
    app._evaluate_v2(state, {"stacks": []}, now_ms)
    rows = journal.shadow_diagnostics("XUSDT", protocol_hash=app.protocol_hash)
    assert len(rows) == 1
    assert rows[0]["status"] == "OBSERVED"
    assert rows[0]["reason"] == "partial_day_observation_only"


def test_signal_path_records_first_touch_once_and_is_not_a_fill(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    signal = PaperSignal("sig-path", 1_000, "XUSDT", Side.LONG, "lane", 98.0, 103.0)

    app._start_signal_path(state, signal, 100.0, 1_000)
    app._observe_signal_paths("XUSDT", 1_100, 101.0)
    app._observe_signal_paths("XUSDT", 1_200, 103.2)
    app._observe_signal_paths("XUSDT", 1_300, 97.0)

    events = app.journal.signal_path_events("sig-path")
    assert [event["status"] for event in events] == ["ACTIVE", "TP_TOUCHED"]
    assert events[-1]["tp_touched"] == "YES"
    assert events[-1]["sl_touched"] == "NO"
    assert events[-1]["provenance"]["not_a_fill"] is True


def test_signal_path_disconnect_and_restart_are_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    app = PaperApp(Journal(database))
    state = SymbolState("XUSDT")
    first = PaperSignal("disconnect-path", 1_000, "XUSDT", Side.LONG, "lane", 98.0, 103.0)
    app._start_signal_path(state, first, 100.0, 1_000)

    asyncio.run(app.handle_message({"op": "connection", "state": "disconnected"}, 1_100))
    assert app.journal.signal_path_events("disconnect-path")[-1]["reason"] == "market_stream_disconnected"

    second = PaperSignal("restart-path", 2_000, "XUSDT", Side.LONG, "lane", 98.0, 103.0)
    app._start_signal_path(state, second, 100.0, 2_000)
    restarted = PaperApp(Journal(database))

    event = restarted.journal.signal_path_events("restart-path")[-1]
    assert event["status"] == "UNKNOWN"
    assert event["reason"] == "restart_without_tick_buffer"
