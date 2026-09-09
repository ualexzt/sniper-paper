from __future__ import annotations

from dataclasses import replace

from sniper_paper.levels import LevelSide
from sniper_paper.market import Bar
from sniper_paper.shadow_levels import GeometryConfig, build_reference_levels, reference_levels

INTERVAL = 60_000


def make_bars(count: int = 120, *, highs: tuple[int, ...] = (), lows: tuple[int, ...] = ()) -> list[Bar]:
    rows: list[Bar] = []
    for index in range(count):
        high = 101.0 if index not in highs else 110.0
        low = 99.0 if index not in lows else 90.0
        rows.append(Bar("XUSDT", INTERVAL, index * INTERVAL, (index + 1) * INTERVAL, 100.0, high, low, 100.0, 1.0, 0.0, 1))
    return rows


def test_reference_geometry_is_causal_and_excludes_rightmost_20_bars() -> None:
    rows = make_bars(highs=(40,))
    before_confirmation = build_reference_levels(rows, timeframe="1m", now_ms=rows[59].closed_at_ms)
    after_confirmation = build_reference_levels(rows, timeframe="1m", now_ms=rows[60].closed_at_ms)

    assert before_confirmation.levels == ()
    assert after_confirmation.levels
    level = after_confirmation.levels[0]
    assert level.origin_at_ms == rows[40].opened_at_ms
    assert level.confirmed_at_ms == rows[60].closed_at_ms
    assert level.first_seen_at_ms == level.confirmed_at_ms
    assert level.confirmed_at_ms <= rows[60].closed_at_ms


def test_future_bars_do_not_change_result_as_of_evaluation_time() -> None:
    rows = make_bars(highs=(40, 80))
    baseline = build_reference_levels(rows[:61], timeframe="1m", now_ms=rows[60].closed_at_ms)
    with_future = build_reference_levels(rows, timeframe="1m", now_ms=rows[60].closed_at_ms)

    assert baseline.levels == with_future.levels
    assert all(level.confirmed_at_ms <= rows[60].closed_at_ms for level in with_future.levels)


def test_wick_or_close_break_rule_omits_already_broken_extreme() -> None:
    rows = make_bars(highs=(40,))
    rows[65] = replace(rows[65], high=111.0, close=100.0)

    close_result = build_reference_levels(rows, timeframe="1m", config=GeometryConfig(break_rule="close"))
    wick_result = build_reference_levels(rows, timeframe="1m", config=GeometryConfig(break_rule="wick"))

    assert any(level.side is LevelSide.HIGH and level.price == 110.0 for level in close_result.levels)
    assert not any(level.side is LevelSide.HIGH and level.price == 110.0 for level in wick_result.levels)


def test_nearby_extremes_merge_with_zone_touch_and_provenance_metadata() -> None:
    result = build_reference_levels(
        make_bars(highs=(40, 80)),
        timeframe="1m",
        config=GeometryConfig(merge_tolerance_bp={"1m": 5.0}),
    )

    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.level_class == "shadow_cluster"
    assert level.touches == 2
    assert level.zone_low == 110.0 == level.zone_high
    assert len(level.provenance["member_level_ids"]) == 2
    assert level.provenance["touch_events"] == [40 * INTERVAL, 80 * INTERVAL]


def test_long_and_short_extremes_are_emitted_symmetrically() -> None:
    levels = reference_levels(make_bars(highs=(40,), lows=(80,)), timeframe="1m")

    assert {level.side for level in levels} == {LevelSide.HIGH, LevelSide.LOW}
    assert {level.price for level in levels} == {110.0, 90.0}


def test_gaps_and_short_history_are_explicit_coverage_metadata() -> None:
    rows = make_bars(70, highs=(30,))
    rows.pop(20)
    result = build_reference_levels(rows, timeframe="1m")

    assert result.coverage.short_history is True
    assert result.coverage.history_complete is False
    assert result.coverage.interior_gap_count == 1
    assert result.coverage.interior_missing_bars == 1
    assert result.coverage.gaps[0]["missing_bars"] == 1
