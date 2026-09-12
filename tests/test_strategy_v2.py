import json
from dataclasses import replace
from pathlib import Path

import pytest

from sniper_paper.levels import DIGASH_LEVEL_TIMEFRAMES, DIGASH_LEVEL_VERSION
from sniper_paper.market import Bar
from sniper_paper.paper import Side
from sniper_paper.strategy_v2 import (
    DecisionStatus,
    LaneName,
    Level,
    LevelSide,
    OrderflowFrame,
    StrategyV2Evaluator,
    _cascade_geometry,
    _LaneCandidate,
    _nearest_target,
)

CENTER = 100.0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_breakout_current_quote_must_stay_beyond_reference(side):
    f = build_failed_sweep_fixture()
    frame = f["confirm_frame"]
    level = Level("ref", "XUSDT", "1m", LevelSide.HIGH, 100.3, 0)
    stop, target = 99.9, 101.0
    if side is Side.SHORT:
        frame, level = mirror_frame(frame), mirror_level(level)
        stop, target = mirror_price(stop), mirror_price(target)
    candidate = _LaneCandidate("test", LaneName.TERMINAL_LEVEL_BREAKOUT.value,
        "XUSDT", side, True, frame.received_at_ms, frame.received_at_ms + 2000,
        level, target, stop, level.price, None, None, 1.0, {})
    result = StrategyV2Evaluator()._trigger(candidate, frame)
    assert result.signal is None
    assert result.reason == "price_returned_through_reference"


def test_frozen_v2_protocol_matches_evaluator_lanes_and_safety_boundary() -> None:
    payload = json.loads((Path(__file__).parents[1] / "paper_strategy_v2.json").read_text())
    assert {row["name"] for row in payload["lanes"]} == {lane.value for lane in LaneName}
    assert payload["safety_boundary"]["paper_only"] is True
    assert payload["safety_boundary"]["api_keys_allowed"] is False
    assert payload["safety_boundary"]["authenticated_orders_allowed"] is False
    diagnostic = {row["name"] for row in payload["lanes"] if row["diagnostic"]}
    assert diagnostic == {
        LaneName.EARLY_TARGET_HUNT.value,
        LaneName.TARGET_SEEKING_BREAKOUT.value,
        LaneName.STRUCTURAL_REACTION.value,
        LaneName.CASCADE_IMPULSE.value,
        LaneName.FRESH_EXTREME_MOMENTUM.value,
        LaneName.DOM_CONFIRMED_BREAKOUT.value,
        LaneName.DIAGONAL_CONTEXT.value,
    }


def bar(tf: int, opened: int, open_: float, high: float, low: float, close: float, delta: float = 1.0) -> Bar:
    return Bar("XUSDT", tf, opened, opened + tf, open_, high, low, close, 1.0, delta, 1)


def mirror_price(price: float) -> float:
    return 2 * CENTER - price


def mirror_bar(row: Bar) -> Bar:
    return Bar(
        row.symbol,
        row.timeframe_ms,
        row.opened_at_ms,
        row.closed_at_ms,
        mirror_price(row.open),
        mirror_price(row.low),
        mirror_price(row.high),
        mirror_price(row.close),
        row.volume,
        -row.delta_notional,
        row.trades,
    )


def mirror_level(level: Level) -> Level:
    return replace(
        level,
        level_id=f"{level.level_id}_m",
        side=LevelSide.LOW if level.side is LevelSide.HIGH else LevelSide.HIGH,
        price=mirror_price(level.price),
        zone_low=mirror_price(level.zone_high if level.zone_high is not None else level.price),
        zone_high=mirror_price(level.zone_low if level.zone_low is not None else level.price),
    )


def mirror_frame(frame: OrderflowFrame) -> OrderflowFrame:
    return replace(
        frame,
        best_bid=mirror_price(frame.best_ask),
        best_ask=mirror_price(frame.best_bid),
        best_bid_size=frame.best_ask_size,
        best_ask_size=frame.best_bid_size,
        delta_notional=-frame.delta_notional,
        top5_bid_notional=frame.top5_ask_notional,
        top5_ask_notional=frame.top5_bid_notional,
        sweep_extreme_price=None if frame.sweep_extreme_price is None else mirror_price(frame.sweep_extreme_price),
        sweep_reference_price=None if frame.sweep_reference_price is None else mirror_price(frame.sweep_reference_price),
        structure_level_price=None if frame.structure_level_price is None else mirror_price(frame.structure_level_price),
        diagonal_slope_bp=-frame.diagonal_slope_bp,
        cascade_body_bp=-frame.cascade_body_bp,
    )


def decision(decisions: list, lane: str):
    return next(item for item in decisions if item.lane == lane)


def build_level_approach_fixture(kind: str, timeframe: str = "1m", mirrored: bool = False) -> dict:
    baseline_15s = [bar(15_000, 900_000 + i * 15_000, 100.0, 100.08, 99.92, 100.0, 10) for i in range(20)]
    bars_1m = [bar(60_000, i * 60_000, 100.0, 100.1, 99.9, 100.0, 10) for i in range(20)]
    if kind == "breakout":
        level = Level("resistance", "XUSDT", timeframe, LevelSide.HIGH, 100.0, 0,
                      level_version=DIGASH_LEVEL_VERSION)
        approach = bar(15_000, 1_200_000, 99.90, 99.98, 99.88, 99.95, 10)
        reaction = bar(15_000, 1_215_000, 99.95, 100.12, 99.94, 100.08, 50)
        frame = OrderflowFrame("XUSDT", 1_230_000, 100.07, 12, 100.08, 5, 50, 20, 18, 12,
                               2_000, 1_000, 0.2, 10, 1)
        lane = LaneName.TERMINAL_LEVEL_BREAKOUT.value
    else:
        level = Level("support", "XUSDT", timeframe, LevelSide.LOW, 100.0, 0,
                      level_version=DIGASH_LEVEL_VERSION)
        approach = bar(15_000, 1_200_000, 100.10, 100.12, 100.02, 100.05, 10)
        reaction = bar(15_000, 1_215_000, 100.05, 100.10, 99.97, 100.06, -50)
        frame = OrderflowFrame("XUSDT", 1_230_000, 100.05, 12, 100.06, 5, -50, 20, 13, 12,
                               2_000, 1_000, 0.2, 10, 1)
        lane = LaneName.FAILED_SWEEP_RECLAIM.value
    bars = {"15s": [*baseline_15s, approach, reaction], "1m": bars_1m}
    if mirrored:
        bars = {tf: [mirror_bar(row) for row in rows] for tf, rows in bars.items()}
        level = mirror_level(level)
        frame = mirror_frame(frame)
    return {"bars": bars, "level": level, "frame": frame, "lane": lane}


@pytest.mark.parametrize("kind", ["breakout", "rejection"])
@pytest.mark.parametrize("mirrored", [False, True])
def test_level_approach_must_precede_orderflow_reaction(kind: str, mirrored: bool) -> None:
    fixture = build_level_approach_fixture(kind, mirrored=mirrored)
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    arm_bars = {**fixture["bars"], "15s": fixture["bars"]["15s"][:-1]}
    arm_frame = replace(fixture["frame"], received_at_ms=1_215_000)
    arm = decision(evaluator.evaluate(symbol="XUSDT", now_ms=1_215_000, bars=arm_bars,
                                      levels=[fixture["level"]], orderflow=arm_frame), fixture["lane"])
    triggered = decision(evaluator.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                                            levels=[fixture["level"]], orderflow=fixture["frame"]), fixture["lane"])
    direct = decision(StrategyV2Evaluator(min_book_imbalance=0.01).evaluate(
        symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"], levels=[fixture["level"]],
        orderflow=fixture["frame"]), fixture["lane"])
    assert arm.signal is None
    assert triggered.status == DecisionStatus.TRIGGERED.value
    assert triggered.features["approach_armed_at_ms"] == 1_215_000
    assert triggered.features["trigger_15s_closed_at_ms"] == 1_230_000
    assert direct.signal is None
    assert evaluator.active_approaches() == []


def test_stale_book_can_arm_approach_but_cannot_trigger() -> None:
    fixture = build_level_approach_fixture("breakout")
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    arm_bars = {**fixture["bars"], "15s": fixture["bars"]["15s"][:-1]}
    stale = replace(fixture["frame"], received_at_ms=1_215_000, book_age_ms=1_000)
    armed = decision(evaluator.evaluate(symbol="XUSDT", now_ms=1_215_000, bars=arm_bars,
                                        levels=[fixture["level"]], orderflow=stale), fixture["lane"])
    assert armed.signal is None
    assert armed.reason == "data_quality"
    assert evaluator.active_approaches()[0]["level_id"] == fixture["level"].level_id
    triggered = decision(evaluator.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                                            levels=[fixture["level"]], orderflow=fixture["frame"]), fixture["lane"])
    assert triggered.status == DecisionStatus.TRIGGERED.value


def test_consumed_or_expired_approach_requires_departure_before_rearm() -> None:
    fixture = build_level_approach_fixture("rejection")
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    arm_bars = {**fixture["bars"], "15s": fixture["bars"]["15s"][:-1]}
    evaluator.evaluate(symbol="XUSDT", now_ms=1_215_000, bars=arm_bars,
                       levels=[fixture["level"]],
                       orderflow=replace(fixture["frame"], received_at_ms=1_215_000))
    evaluator.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                       levels=[fixture["level"]], orderflow=fixture["frame"])
    assert evaluator.active_approaches() == []
    evaluator.evaluate(symbol="XUSDT", now_ms=1_245_000, bars=fixture["bars"],
                       levels=[fixture["level"]], orderflow=replace(fixture["frame"], received_at_ms=1_245_000))
    assert evaluator.active_approaches() == []

    departed = bar(15_000, 1_230_000, 100.06, 100.55, 100.05, 100.50, 5)
    evaluator.evaluate(symbol="XUSDT", now_ms=1_245_000,
                       bars={**fixture["bars"], "15s": [*fixture["bars"]["15s"], departed]},
                       levels=[fixture["level"]], orderflow=replace(fixture["frame"], received_at_ms=1_245_000))
    returned = bar(15_000, 1_245_000, 100.50, 100.52, 100.02, 100.05, 5)
    evaluator.evaluate(symbol="XUSDT", now_ms=1_260_000,
                       bars={**fixture["bars"], "15s": [*fixture["bars"]["15s"], departed, returned]},
                       levels=[fixture["level"]], orderflow=replace(fixture["frame"], received_at_ms=1_260_000))
    assert evaluator.active_approaches()[0]["level_id"] == fixture["level"].level_id


@pytest.mark.parametrize("kind", ["breakout", "rejection"])
def test_orderflow_reaction_rejects_weak_flow_and_invalidated_level(kind: str) -> None:
    fixture = build_level_approach_fixture(kind)
    arm_bars = {**fixture["bars"], "15s": fixture["bars"]["15s"][:-1]}
    weak = StrategyV2Evaluator(min_book_imbalance=0.01)
    arm_frame = replace(fixture["frame"], received_at_ms=1_215_000)
    weak.evaluate(symbol="XUSDT", now_ms=1_215_000, bars=arm_bars,
                  levels=[fixture["level"]], orderflow=arm_frame)
    weak_frame = replace(fixture["frame"], delta_notional=1)
    assert decision(weak.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                                  levels=[fixture["level"]], orderflow=weak_frame), fixture["lane"]).signal is None
    invalidated = StrategyV2Evaluator(min_book_imbalance=0.01)
    invalidated.evaluate(symbol="XUSDT", now_ms=1_215_000, bars=arm_bars,
                         levels=[fixture["level"]], orderflow=arm_frame)
    broken = replace(fixture["level"], broken_at_ms=1_220_000)
    assert decision(invalidated.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                                         levels=[broken], orderflow=fixture["frame"]), fixture["lane"]).signal is None


@pytest.mark.parametrize("timeframe", sorted(DIGASH_LEVEL_TIMEFRAMES))
def test_orderflow_breakout_accepts_every_runtime_level_timeframe(timeframe: str) -> None:
    fixture = build_level_approach_fixture("breakout", timeframe=timeframe)
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    evaluator.evaluate(symbol="XUSDT", now_ms=1_215_000,
                       bars={**fixture["bars"], "15s": fixture["bars"]["15s"][:-1]},
                       levels=[fixture["level"]], orderflow=replace(fixture["frame"], received_at_ms=1_215_000))
    result = decision(evaluator.evaluate(symbol="XUSDT", now_ms=1_230_000, bars=fixture["bars"],
                                         levels=[fixture["level"]], orderflow=fixture["frame"]), fixture["lane"])
    assert result.status == DecisionStatus.TRIGGERED.value
    assert result.target_level.timeframe == timeframe


def trigger_candidate(side: Side, risk_bp: float, lane: str = LaneName.TERMINAL_LEVEL_BREAKOUT.value) -> _LaneCandidate:
    entry = 100.0 if side is Side.LONG else 100.1
    stop = entry * (1 - risk_bp / 10_000) if side is Side.LONG else entry * (1 + risk_bp / 10_000)
    target = 101.0 if side is Side.LONG else 99.0
    return _LaneCandidate(
        setup_id=f"{lane}-{side.value}-{risk_bp}", lane=lane, symbol="XUSDT", side=side,
        trigger_now=True, armed_at_ms=1, expires_at_ms=2,
        target_level=Level("reference", "XUSDT", "1m", LevelSide.HIGH,
                           99.99 if side is Side.LONG else 100.11, 0),
        target_price=target, stop_price=stop, invalidation_price=stop,
        reference_price=None, sweep_extreme_price=None, score=1.0, features={},
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("risk_bp", [10.0, 50.0])
def test_trigger_accepts_inclusive_risk_boundaries(side: Side, risk_bp: float) -> None:
    frame = OrderflowFrame("XUSDT", 1, 100.0, 10.0, 100.1, 10.0, 0, 1, 1, 1, 2_000, 2_000, 1, 1, 1)
    result = StrategyV2Evaluator()._trigger(trigger_candidate(side, risk_bp), frame)
    assert result.status == DecisionStatus.TRIGGERED.value


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_trigger_rejects_risk_outside_bounds_for_all_executable_lane_shapes(side: Side) -> None:
    frame = OrderflowFrame("XUSDT", 1, 100.0, 10.0, 100.1, 10.0, 0, 1, 1, 1, 2_000, 2_000, 1, 1, 1)
    for lane in (LaneName.CASCADE_IMPULSE.value, LaneName.FRESH_EXTREME_MOMENTUM.value, LaneName.STRUCTURAL_REACTION.value):
        result = StrategyV2Evaluator()._trigger(trigger_candidate(side, 50.01, lane), frame)
        assert result.status == DecisionStatus.REJECTED.value
        assert result.reason == "diagnostic_only"


def test_trigger_rejects_live_ena_inverted_short_bracket() -> None:
    frame = OrderflowFrame("ENAUSDT", 1, 0.15060, 10.0, 0.15062, 10.0, 0, 1, 1, 1, 2_000, 2_000, 1, 1, 1)
    candidate = trigger_candidate(Side.SHORT, 20.0, "cascade_impulse")
    candidate = candidate.__class__(**{**candidate.__dict__, "symbol": "ENAUSDT", "stop_price": 0.15061527, "target_price": 0.14622})
    result = StrategyV2Evaluator()._trigger(candidate, frame)
    assert result.status == DecisionStatus.REJECTED.value
    assert result.reason == "diagnostic_only"


def build_failed_sweep_fixture():
    bars_15s = [
        bar(15_000, 600_000 + i * 15_000, 100.08, 100.18, 99.98, 100.12, 18.0)
        for i in range(20)
    ]
    sweep = bar(15_000, 900_000, 100.12, 100.15, 99.78, 100.05, -80.0)
    confirm = bar(15_000, 915_000, 100.05, 100.42, 100.02, 100.36, 90.0)
    bars_1m = [
        bar(60_000, i * 60_000, 99.96 + i * 0.02, 100.15 + i * 0.02, 99.88 + i * 0.02, 100.02 + i * 0.02, 10.0)
        for i in range(15)
    ]
    # The arm must come from the completed 1m candle, not the 15s sweep.
    bars_1m[-1] = bar(60_000, 840_000, 100.30, 100.36, 99.78, 100.05, -80.0)
    level = Level("support", "XUSDT", "15m", LevelSide.LOW, 100.0, 0)
    arm_frame = OrderflowFrame(
        "XUSDT",
        915_000,
        100.01,
        8.0,
        100.03,
        6.0,
        -80.0,
        20.0,
        8.0,
        20.0,
        1_500.0,
        1_100.0,
        0.6,
        250,
        1.5,
        sweep_extreme_price=99.78,
        sweep_reference_price=100.0,
    )
    confirm_frame = OrderflowFrame(
        "XUSDT",
        930_000,
        100.18,
        12.0,
        100.20,
        5.0,
        90.0,
        20.0,
        8.0,
        20.0,
        1_700.0,
        900.0,
        0.6,
        250,
        1.8,
        sweep_extreme_price=99.78,
        sweep_reference_price=100.0,
    )
    return {
        "bars": {"15s": [*bars_15s, sweep, confirm], "1m": bars_1m},
        "levels": [level],
        "arm_frame": arm_frame,
        "confirm_frame": confirm_frame,
        "arm_now": 915_000,
        "confirm_now": 930_000,
    }


def build_breakout_fixture(include_future: bool) -> dict:
    four = [
        bar(14_400_000, i * 14_400_000, 100.0 + i, 100.6 + i, 99.8 + i, 100.3 + i, 8.0)
        for i in range(7)
    ]
    five = [
        bar(300_000, i * 300_000, 105.0, 105.4, 104.7, 105.1, 3.0)
        for i in range(6)
    ]
    one = [
        bar(60_000, i * 60_000, 105.0, 105.2, 104.9, 105.1 + i * 0.05, 10.0)
        for i in range(3)
    ]
    one.append(bar(60_000, 180_000, 105.15, 105.30, 105.00, 105.18, 12.0))
    if include_future:
        one.append(bar(60_000, 240_000, 105.18, 106.20, 105.10, 106.10, 14.0))
    levels = [Level("target", "XUSDT", "4h", LevelSide.HIGH, 110.0, 0)]
    frame = OrderflowFrame(
        "XUSDT",
        180_000,
        105.18,
        9.0,
        105.20,
        7.0,
        18.0,
        5.0,
        6.0,
        8.0,
        1_400.0,
        800.0,
        0.7,
        220,
        1.6,
    )
    if include_future:
        frame = replace(frame, received_at_ms=180_000)
    return {"bars": {"4h": four, "5m": five, "1m": one}, "levels": levels, "frame": frame, "now": 180_000}


def build_structural_fixture():
    bars_15m = [
        bar(900_000, 0, 100.0, 100.12, 99.90, 100.00, 6.0),
        bar(900_000, 900_000, 100.00, 100.30, 99.82, 100.22, 9.0),
    ]
    levels = [Level("resistance", "XUSDT", "15m", LevelSide.HIGH, 101.0, 0)]
    frame = OrderflowFrame(
        "XUSDT",
        1_800_000,
        100.18,
        11.0,
        100.20,
        6.0,
        14.0,
        6.0,
        8.0,
        10.0,
        1_500.0,
        900.0,
        0.5,
        250,
        1.7,
        structure_level_price=100.0,
    )
    return {"bars": {"15m": bars_15m}, "levels": levels, "frame": frame, "now": 1_800_000}


def test_lane_inventory_and_future_bars_are_ignored() -> None:
    baseline = build_breakout_fixture(include_future=False)
    future = build_breakout_fixture(include_future=True)
    evaluator_a = StrategyV2Evaluator(min_book_imbalance=0.01)
    evaluator_b = StrategyV2Evaluator(min_book_imbalance=0.01)
    decisions_a = evaluator_a.evaluate(
        symbol="XUSDT",
        now_ms=baseline["now"],
        bars=baseline["bars"],
        levels=baseline["levels"],
        orderflow=baseline["frame"],
    )
    decisions_b = evaluator_b.evaluate(
        symbol="XUSDT",
        now_ms=future["now"],
        bars=future["bars"],
        levels=future["levels"],
        orderflow=future["frame"],
    )
    expected = {lane.value for lane in LaneName}
    assert {item.lane for item in decisions_a} == expected
    assert {item.lane for item in decisions_b} == expected
    assert decision(decisions_a, LaneName.DOM_CONFIRMED_BREAKOUT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions_a, LaneName.DIAGONAL_CONTEXT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions_a, LaneName.DOM_CONFIRMED_BREAKOUT.value).signal is None
    assert decision(decisions_a, LaneName.DIAGONAL_CONTEXT.value).signal is None
    assert decision(decisions_a, LaneName.EARLY_TARGET_HUNT.value).status == decision(
        decisions_b, LaneName.EARLY_TARGET_HUNT.value
    ).status
    assert decision(decisions_a, LaneName.EARLY_TARGET_HUNT.value).reason == decision(
        decisions_b, LaneName.EARLY_TARGET_HUNT.value
    ).reason
    assert decision(decisions_a, LaneName.TARGET_SEEKING_BREAKOUT.value).status == decision(
        decisions_b, LaneName.TARGET_SEEKING_BREAKOUT.value
    ).status
    assert decision(decisions_a, LaneName.TARGET_SEEKING_BREAKOUT.value).reason == decision(
        decisions_b, LaneName.TARGET_SEEKING_BREAKOUT.value
    ).reason


def test_nearest_target_is_symmetric_for_both_sides() -> None:
    levels = [
        Level("low_far", "XUSDT", "15m", LevelSide.LOW, 97.5, 0),
        Level("low_near", "XUSDT", "15m", LevelSide.LOW, 99.2, 0),
        Level("high_near", "XUSDT", "15m", LevelSide.HIGH, 100.8, 0),
        Level("high_far", "XUSDT", "15m", LevelSide.HIGH, 103.0, 0),
    ]
    long_target = _nearest_target(
        levels,
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        now_ms=1_000,
        timeframes={"15m"},
    )
    short_target = _nearest_target(
        levels,
        symbol="XUSDT",
        side=Side.SHORT,
        price=100.0,
        now_ms=1_000,
        timeframes={"15m"},
    )
    assert long_target is not None
    assert short_target is not None
    assert long_target.price == pytest.approx(100.8)
    assert short_target.price == pytest.approx(99.2)


def test_canonical_target_selection_accepts_digash_levels_from_every_timeframe() -> None:
    for timeframe in sorted(DIGASH_LEVEL_TIMEFRAMES):
        level = Level(
            f"digash-{timeframe}",
            "XUSDT",
            timeframe,
            LevelSide.HIGH,
            100.8,
            0,
            level_class="digash_extreme",
            level_version=DIGASH_LEVEL_VERSION,
        )
        target = _nearest_target(
            [level],
            symbol="XUSDT",
            side=Side.LONG,
            price=100.0,
            now_ms=1_000,
            timeframes=DIGASH_LEVEL_TIMEFRAMES,
            canonical=True,
        )
        assert target is not None
        assert target.timeframe == timeframe


def test_nearest_target_skips_levels_broken_as_of_evaluation_time() -> None:
    levels = [
        Level("broken", "XUSDT", "15m", LevelSide.HIGH, 100.8, 0, broken_at_ms=900),
        Level("active", "XUSDT", "15m", LevelSide.HIGH, 101.2, 0),
    ]
    target = _nearest_target(
        levels,
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        now_ms=1_000,
        timeframes={"15m"},
    )
    assert target is not None
    assert target.level_id == "active"


def test_nearest_target_can_reference_level_active_before_trigger_break() -> None:
    levels = [
        Level("a", "XUSDT", "15m", LevelSide.HIGH, 100.8, 0, broken_at_ms=60_000),
        Level("b", "XUSDT", "15m", LevelSide.HIGH, 100.85, 0, broken_at_ms=60_000),
    ]
    target = _nearest_target(
        levels,
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        now_ms=75_000,
        active_as_of_ms=59_999,
        timeframes={"15m"},
        canonical=True,
    )
    assert target is not None
    assert target.level_class == "cluster"
    assert target.price == pytest.approx(100.8)
    assert _nearest_target(
        levels,
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        now_ms=75_000,
        timeframes={"15m"},
        canonical=True,
    ) is None


def test_trigger_rejects_reference_level_broken_before_arm() -> None:
    evaluator = StrategyV2Evaluator()
    candidate = trigger_candidate(Side.LONG, 20.0)
    candidate = candidate.__class__(
        **{
            **candidate.__dict__,
            "armed_at_ms": 100,
            "target_level": Level("old", "XUSDT", "1m", LevelSide.HIGH, 100.5, 0, broken_at_ms=99),
        }
    )
    frame = OrderflowFrame("XUSDT", 100, 100, 10, 100.1, 10, 100, 1, 1, 1, 2_000, 1_000, 1, 1, 1)
    result = evaluator._trigger(candidate, frame)
    assert result.signal is None
    assert result.reason == "invalid_reference_level_lifecycle"


def test_non_executable_impulse_lanes_never_emit_signals() -> None:
    frame = OrderflowFrame("XUSDT", 100, 100, 10, 100.1, 10, 100, 1, 20, 10, 2_000, 1_000, .5, 10, 1)
    decisions = StrategyV2Evaluator().evaluate(symbol="XUSDT", now_ms=100, bars={}, levels=[], orderflow=frame)
    for lane in (LaneName.EARLY_TARGET_HUNT, LaneName.TARGET_SEEKING_BREAKOUT,
                 LaneName.STRUCTURAL_REACTION, LaneName.CASCADE_IMPULSE,
                 LaneName.FRESH_EXTREME_MOMENTUM):
        result = decision(decisions, lane.value)
        assert result.signal is None
        assert result.reason == "diagnostic_only"


def test_cascade_geometry_records_ordered_distinct_levels_without_a_hidden_gate() -> None:
    levels = [
        Level("near", "XUSDT", "4h", LevelSide.HIGH, 101.0, 0),
        Level("far", "XUSDT", "4h", LevelSide.HIGH, 103.0, 0),
        Level("behind", "XUSDT", "4h", LevelSide.LOW, 99.0, 0),
    ]

    result = _cascade_geometry(levels, symbol="XUSDT", side=Side.LONG, price=100.0, now_ms=1)

    assert result["cascade_count"] == 2.0
    assert result["cascade_level_ids"] == "near,far"
    assert result["cascade_first_level_id"] == "near"
    assert result["cascade_last_level_id"] == "far"
    assert result["cascade_total_span_bp"] == pytest.approx((103 / 101 - 1) * 10_000)


def test_lane_separation_keeps_diagnostic_lanes_non_trading() -> None:
    sweep = build_failed_sweep_fixture()
    structural = build_structural_fixture()
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    evaluator.evaluate(
        symbol="XUSDT",
        now_ms=sweep["arm_now"],
        bars={"15s": sweep["bars"]["15s"][:-1], "1m": sweep["bars"]["1m"]},
        levels=sweep["levels"],
        orderflow=sweep["arm_frame"],
    )
    decisions = evaluator.evaluate(
        symbol="XUSDT",
        now_ms=structural["now"],
        bars=structural["bars"],
        levels=[*sweep["levels"], *structural["levels"]],
        orderflow=structural["frame"],
    )
    assert decision(decisions, LaneName.STRUCTURAL_REACTION.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.STRUCTURAL_REACTION.value).reason == "diagnostic_only"
    assert decision(decisions, LaneName.FAILED_SWEEP_RECLAIM.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DOM_CONFIRMED_BREAKOUT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DIAGONAL_CONTEXT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DOM_CONFIRMED_BREAKOUT.value).signal is None
    assert decision(decisions, LaneName.DIAGONAL_CONTEXT.value).signal is None
