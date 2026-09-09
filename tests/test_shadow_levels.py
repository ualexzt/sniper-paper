from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

from sniper_paper.levels import LevelSide
from sniper_paper.market import Bar
from sniper_paper.shadow_levels import (
    CANONICAL_TIMEFRAMES,
    DEFAULT_MERGE_TOLERANCE_BP,
    GeometryConfig,
    build_digash_levels,
    build_reference_levels,
    reference_levels,
)

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
    rows[60] = replace(rows[60], high=111.0, close=100.0)

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
    assert level.level_class == "digash_cluster"
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
    assert not any(level.price == 110.0 for level in result.levels)


def test_canonical_timeframes_and_versioned_tolerance_hypothesis() -> None:
    assert CANONICAL_TIMEFRAMES == ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
    assert DEFAULT_MERGE_TOLERANCE_BP["1m"] == 20.0
    assert DEFAULT_MERGE_TOLERANCE_BP["1d"] == 125.0
    assert all(
        DEFAULT_MERGE_TOLERANCE_BP[left] < DEFAULT_MERGE_TOLERANCE_BP[right]
        for left, right in pairwise(CANONICAL_TIMEFRAMES)
    )


def test_detector_caps_to_latest_1000_completed_bars_and_has_canonical_alias() -> None:
    rows = make_bars(1_080, highs=(40, 1_020))
    result = build_digash_levels(rows, timeframe="1m")
    assert result.coverage.received_bars == 1_080
    assert result.coverage.used_bars == 1_000
    assert all(level.origin_at_ms >= rows[80].opened_at_ms for level in result.levels)


def test_post_confirmation_break_is_left_for_instrument_aware_runtime_lifecycle() -> None:
    rows = make_bars(120, highs=(40,))
    rows[65] = replace(rows[65], close=111.0, high=111.0)
    before_break = build_reference_levels(rows, timeframe="1m", now_ms=rows[60].closed_at_ms)
    after_break = build_reference_levels(rows, timeframe="1m", now_ms=rows[66].closed_at_ms)
    assert any(level.price == 110.0 for level in before_break.levels)
    assert any(level.price == 110.0 for level in after_break.levels)


def test_lifecycle_breaks_constituent_before_merge_and_never_resurrects_cluster() -> None:
    """A broken 110 pivot must not revive when the 110.02 pivot is confirmed."""
    rows = make_bars(120, highs=(40,))
    rows[65] = replace(rows[65], high=110.02, close=110.02)

    before = build_reference_levels(rows[:67], timeframe="1m", now_ms=rows[66].closed_at_ms,
                                    tick_size=0.01)
    assert any(level.price == 110.0 and level.broken_at_ms is not None for level in before.levels)

    after = build_reference_levels(rows, timeframe="1m", now_ms=rows[86].closed_at_ms,
                                   tick_size=0.01, persisted_levels=before.levels)
    old = [level for level in after.levels if level.origin_at_ms == rows[40].opened_at_ms]
    fresh = [level for level in after.levels if level.origin_at_ms == rows[65].opened_at_ms]
    # 1m is the source timeframe here, so the configured source rule is one
    # completed close (the fast two-close rule applies to slower levels).
    assert old and old[0].broken_at_ms == rows[65].closed_at_ms
    assert fresh and fresh[0].broken_at_ms is None
    assert not any(level.level_class == "digash_cluster" and level.broken_at_ms is None for level in after.levels)


def test_lifecycle_restart_replay_preserves_broken_constituent_tombstone() -> None:
    rows = make_bars(120, highs=(40,))
    rows[65] = replace(rows[65], high=110.02, close=110.02)
    continuous = build_reference_levels(rows, timeframe="1m", now_ms=rows[86].closed_at_ms,
                                         tick_size=0.01)
    first = build_reference_levels(rows[:67], timeframe="1m", now_ms=rows[66].closed_at_ms,
                                   tick_size=0.01)
    restarted = build_reference_levels(rows, timeframe="1m", now_ms=rows[86].closed_at_ms,
                                        tick_size=0.01, persisted_levels=first.levels)
    assert [(level.level_id, level.broken_at_ms) for level in restarted.levels] == [
        (level.level_id, level.broken_at_ms) for level in continuous.levels
    ]


def test_lifecycle_uses_actual_fast_bars_for_slower_source_level() -> None:
    four_hour_ms = 14_400_000
    rows = [
        Bar("XUSDT", four_hour_ms, i * four_hour_ms, (i + 1) * four_hour_ms,
            100.0, 110.0 if i == 40 else 101.0, 99.0, 100.0, 1.0, 0.0, 1)
        for i in range(61)
    ]
    start = rows[60].closed_at_ms
    fast = [
        Bar("XUSDT", INTERVAL, start + i * INTERVAL, start + (i + 1) * INTERVAL,
            100.0, 110.02, 99.0, 110.02, 1.0, 0.0, 1)
        for i in range(2)
    ]
    result = build_reference_levels(
        rows, timeframe="4h", now_ms=fast[-1].closed_at_ms, tick_size=0.01,
        lifecycle_bars={"4h": rows, "1m": fast}, fast_timeframe="1m",
        fast_confirming_closes=2, source_confirming_closes=1,
    )
    level = next(level for level in result.levels if level.price == 110.0)
    assert level.broken_at_ms == fast[-1].closed_at_ms
