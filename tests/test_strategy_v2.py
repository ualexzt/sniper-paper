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
    _nearest_target,
)

CENTER = 100.0


def test_frozen_v2_protocol_matches_evaluator_lanes_and_safety_boundary() -> None:
    payload = json.loads((Path(__file__).parents[1] / "paper_strategy_v2.json").read_text())
    assert {row["name"] for row in payload["lanes"]} == {lane.value for lane in LaneName}
    assert payload["safety_boundary"]["paper_only"] is True
    assert payload["safety_boundary"]["api_keys_allowed"] is False
    assert payload["safety_boundary"]["authenticated_orders_allowed"] is False
    diagnostic = {row["name"] for row in payload["lanes"] if row["diagnostic"]}
    assert diagnostic == {LaneName.DOM_CONFIRMED_BREAKOUT.value, LaneName.DIAGONAL_CONTEXT.value}


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
    return Level(
        f"{level.level_id}_m",
        level.symbol,
        level.timeframe,
        LevelSide.LOW if level.side is LevelSide.HIGH else LevelSide.HIGH,
        mirror_price(level.price),
        level.confirmed_at_ms,
        level.touches,
        level.level_class,
        level.origin_at_ms,
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


def test_terminal_lane_can_trade_the_first_break_but_not_reuse_the_level() -> None:
    bars = {
        "1m": [
            bar(60_000, 0, 100.0, 100.2, 99.8, 100.0, 10.0),
            bar(60_000, 60_000, 100.0, 101.2, 99.9, 101.0, 20.0),
        ]
    }
    levels = [
        Level("a", "XUSDT", "15m", LevelSide.HIGH, 100.80, 0, broken_at_ms=120_000),
        Level("b", "XUSDT", "15m", LevelSide.HIGH, 100.81, 0, broken_at_ms=120_000),
    ]
    frame = OrderflowFrame(
        symbol="XUSDT",
        received_at_ms=120_000,
        best_bid=100.90,
        best_bid_size=10.0,
        best_ask=100.91,
        best_ask_size=5.0,
        delta_notional=20.0,
        median_abs_delta_20=5.0,
        range_bp=20.0,
        median_range_bp_20=10.0,
        top5_bid_notional=2_000.0,
        top5_ask_notional=1_000.0,
        atr_1m=0.5,
        book_age_ms=10,
        spread_bp=1.0,
    )
    evaluator = StrategyV2Evaluator(min_risk_bp=1.0, max_risk_bp=200.0)

    first = decision(
        evaluator.evaluate(symbol="XUSDT", now_ms=120_000, bars=bars, levels=levels, orderflow=frame),
        LaneName.TERMINAL_LEVEL_BREAKOUT.value,
    )
    later = decision(
        StrategyV2Evaluator(min_risk_bp=1.0, max_risk_bp=200.0).evaluate(
            symbol="XUSDT",
            now_ms=180_000,
            bars=bars,
            levels=levels,
            orderflow=replace(frame, received_at_ms=180_000),
        ),
        LaneName.TERMINAL_LEVEL_BREAKOUT.value,
    )

    assert first.status == DecisionStatus.TRIGGERED.value
    assert first.target_level is not None
    assert first.target_level.level_class == "cluster"
    assert later.status == DecisionStatus.REJECTED.value


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


def test_failed_sweep_lane_cannot_reuse_broken_structural_level() -> None:
    fixture = build_failed_sweep_fixture()
    broken_levels = [replace(fixture["levels"][0], broken_at_ms=900_000)]
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)

    result = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=fixture["arm_now"],
            bars={"15s": fixture["bars"]["15s"][:-1], "1m": fixture["bars"]["1m"]},
            levels=broken_levels,
            orderflow=fixture["arm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )

    assert result.status == DecisionStatus.REJECTED.value
    assert result.reason == "no_sweep_setup"


def test_failed_sweep_is_symmetric() -> None:
    long_fixture = build_failed_sweep_fixture()
    short_fixture = {
        "bars": {
            key: [mirror_bar(row) for row in value]
            for key, value in long_fixture["bars"].items()
        },
        "levels": [mirror_level(item) for item in long_fixture["levels"]],
        "arm_frame": mirror_frame(long_fixture["arm_frame"]),
        "confirm_frame": mirror_frame(long_fixture["confirm_frame"]),
        "arm_now": long_fixture["arm_now"],
        "confirm_now": long_fixture["confirm_now"],
    }
    long_eval = StrategyV2Evaluator(min_book_imbalance=0.01)
    short_eval = StrategyV2Evaluator(min_book_imbalance=0.01)
    long_eval.evaluate(
        symbol="XUSDT",
        now_ms=long_fixture["arm_now"],
        bars={"15s": long_fixture["bars"]["15s"][:-1], "1m": long_fixture["bars"]["1m"]},
        levels=long_fixture["levels"],
        orderflow=long_fixture["arm_frame"],
    )
    short_eval.evaluate(
        symbol="XUSDT",
        now_ms=short_fixture["arm_now"],
        bars={"15s": short_fixture["bars"]["15s"][:-1], "1m": short_fixture["bars"]["1m"]},
        levels=short_fixture["levels"],
        orderflow=short_fixture["arm_frame"],
    )
    long_decision = decision(
        long_eval.evaluate(
            symbol="XUSDT",
            now_ms=long_fixture["confirm_now"],
            bars=long_fixture["bars"],
            levels=long_fixture["levels"],
            orderflow=long_fixture["confirm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    short_decision = decision(
        short_eval.evaluate(
            symbol="XUSDT",
            now_ms=short_fixture["confirm_now"],
            bars=short_fixture["bars"],
            levels=short_fixture["levels"],
            orderflow=short_fixture["confirm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    assert long_decision.status == DecisionStatus.TRIGGERED.value
    assert short_decision.status == DecisionStatus.TRIGGERED.value
    assert long_decision.side is Side.LONG
    assert short_decision.side is Side.SHORT
    assert mirror_price(long_decision.stop_price) == pytest.approx(short_decision.stop_price)
    assert mirror_price(long_decision.target_price) == pytest.approx(short_decision.target_price)


def test_failed_sweep_confirmation_uses_delta_baseline_frozen_at_arm() -> None:
    fixture = build_failed_sweep_fixture()
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    evaluator.evaluate(
        symbol="XUSDT",
        now_ms=fixture["arm_now"],
        bars={"15s": fixture["bars"]["15s"][:-1], "1m": fixture["bars"]["1m"]},
        levels=fixture["levels"],
        orderflow=fixture["arm_frame"],
    )
    changed_baseline = replace(fixture["confirm_frame"], median_abs_delta_20=1_000.0)

    result = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=fixture["confirm_now"],
            bars=fixture["bars"],
            levels=fixture["levels"],
            orderflow=changed_baseline,
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )

    assert result.status == DecisionStatus.TRIGGERED.value
    assert result.features["confirmation_frozen_delta_baseline"] == 20.0


def test_one_attempt_turns_repeat_into_missed() -> None:
    fixture = build_failed_sweep_fixture()
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    evaluator.evaluate(
        symbol="XUSDT",
        now_ms=fixture["arm_now"],
        bars={"15s": fixture["bars"]["15s"][:-1], "1m": fixture["bars"]["1m"]},
        levels=fixture["levels"],
        orderflow=fixture["arm_frame"],
    )
    first = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=fixture["confirm_now"],
            bars=fixture["bars"],
            levels=fixture["levels"],
            orderflow=fixture["confirm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    repeat = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=fixture["confirm_now"],
            bars=fixture["bars"],
            levels=fixture["levels"],
            orderflow=fixture["confirm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    assert first.status == DecisionStatus.TRIGGERED.value
    assert repeat.status == DecisionStatus.MISSED.value
    assert repeat.reason == "setup_already_attempted"


def test_pending_setup_expires_to_missed() -> None:
    fixture = build_failed_sweep_fixture()
    evaluator = StrategyV2Evaluator(min_book_imbalance=0.01)
    armed = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=fixture["arm_now"],
            bars={"15s": fixture["bars"]["15s"][:-1], "1m": fixture["bars"]["1m"]},
            levels=fixture["levels"],
            orderflow=fixture["arm_frame"],
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    stale_frame = replace(fixture["arm_frame"], received_at_ms=950_000)
    missed = decision(
        evaluator.evaluate(
            symbol="XUSDT",
            now_ms=950_000,
            bars={"15s": fixture["bars"]["15s"][:-1], "1m": fixture["bars"]["1m"]},
            levels=fixture["levels"],
            orderflow=stale_frame,
        ),
        LaneName.FAILED_SWEEP_RECLAIM.value,
    )
    assert armed.status == DecisionStatus.REJECTED.value
    assert armed.reason == "armed_pending_confirmation"
    assert missed.status == DecisionStatus.MISSED.value
    assert missed.reason == "setup_expired"


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
    assert decision(decisions, LaneName.STRUCTURAL_REACTION.value).status == DecisionStatus.TRIGGERED.value
    assert decision(decisions, LaneName.FAILED_SWEEP_RECLAIM.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DOM_CONFIRMED_BREAKOUT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DIAGONAL_CONTEXT.value).status == DecisionStatus.REJECTED.value
    assert decision(decisions, LaneName.DOM_CONFIRMED_BREAKOUT.value).signal is None
    assert decision(decisions, LaneName.DIAGONAL_CONTEXT.value).signal is None
