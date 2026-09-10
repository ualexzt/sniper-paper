from dataclasses import replace

from sniper_paper.levels import LevelSide, canonical_level_catalog, canonical_level_view
from sniper_paper.market import Bar
from sniper_paper.shadow_levels import GeometryConfig, build_reference_levels

MS = 60_000


def bars(*, highs=(), lows=(), closes=None, count=120):
    closes = closes or {}
    return [Bar("XUSDT", MS, i * MS, (i + 1) * MS, 100,
                 highs.get(i, 101) if isinstance(highs, dict) else (110 if i in highs else 101),
                 lows.get(i, 99) if isinstance(lows, dict) else (90 if i in lows else 99),
                 closes.get(i, 100), 1, 0, 1) for i in range(count)]


def high_levels(rows, **kwargs):
    return [x for x in build_reference_levels(rows, timeframe="1m", **kwargs).levels if x.side is LevelSide.HIGH]


def test_equal_separated_peaks_are_two_touch_episodes():
    level = high_levels(bars(highs=(40, 50)))[0]
    assert level.touches == 2
    assert level.provenance["touch_events"] == [40 * MS, 50 * MS]


def test_separated_seed_candidates_without_departure_are_one_episode():
    rows = bars(highs={40: 110, 50: 109.95}, closes={i: 109.99 for i in range(41, 51)})
    level = high_levels(rows)[0]
    assert level.touches == 1
    assert len(level.provenance["touch_events"]) == 1


def test_lower_near_return_within_merge_tolerance_is_touch():
    level = high_levels(bars(highs={40: 110, 50: 109.95}))[0]
    assert level.touches == 2
    assert level.provenance["touch_tolerance_bp"] == 20.0


def test_adjacent_plateau_is_one_event_and_flat_baseline_is_not_level():
    assert high_levels(bars(highs=(40, 41)))[0].touches == 1
    assert high_levels(bars(highs=())) == []


def test_touch_events_match_touch_count_and_low_is_symmetric():
    for side in (LevelSide.HIGH, LevelSide.LOW):
        rows = bars(highs=(40, 80)) if side is LevelSide.HIGH else bars(lows=(40, 80))
        level = next(x for x in build_reference_levels(rows, timeframe="1m").levels if x.side is side)
        assert len(level.provenance["touch_events"]) == level.touches == 2


def test_touch_events_stop_at_causal_break():
    rows = bars(highs=(40, 80), closes={70: 111})
    level = high_levels(rows)[0]
    assert level.provenance["touch_events"] == [40 * MS]
    assert level.touches == 1


def test_restart_recompute_has_identical_ids_touches_and_events():
    rows = bars(highs=(40, 80))
    first = build_reference_levels(rows, timeframe="1m")
    second = build_reference_levels(rows, timeframe="1m", persisted_levels=first.levels)
    assert [(x.level_id, x.touches, x.provenance["touch_events"]) for x in first.levels] == [
        (x.level_id, x.touches, x.provenance["touch_events"]) for x in second.levels
    ]


def test_display_grouping_does_not_change_trading_catalog_or_sum_touches():
    rows = bars(highs=(40, 80))
    one = high_levels(rows)[0]
    five = replace(one, timeframe="5m", level_id="other-tf", confirmed_at_ms=one.confirmed_at_ms)
    source = [one, five]
    catalog = canonical_level_catalog(source, "XUSDT", 10_000_000)
    view = canonical_level_view(source, "XUSDT", 110, 10_000_000)
    assert len(catalog) == 2
    assert len(view) == 1
    assert view[0].touches == one.touches
    assert view[0].provenance["display_timeframes"] == ["1m", "5m"]


def test_cluster_ray_anchor_uses_boundary_price_origin():
    rows = bars(highs={40: 110.1, 80: 110})
    level = high_levels(rows, config=GeometryConfig(merge_tolerance_bp={"1m": 20.0}))[0]
    assert level.price == 110
    assert level.provenance["ray_anchor_at_ms"] == 80 * MS
    assert level.origin_at_ms == 40 * MS
