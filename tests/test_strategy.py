from sniper_paper.market import Bar
from sniper_paper.paper import Side
from sniper_paper.strategy import (
    CausalLevelEngine,
    Level,
    LevelSide,
    StrategyEvaluator,
    apply_level_breaks,
    current_display_levels,
    previous_utc_day_levels,
)


def bar(tf: int, opened: int, o: float, h: float, l: float, c: float, delta: float = 1) -> Bar:
    return Bar("XUSDT", tf, opened, opened + tf, o, h, l, c, 1, delta, 1)


def test_level_is_confirmed_only_after_two_right_bars() -> None:
    engine = CausalLevelEngine("15m")
    rows = [bar(900_000, i * 900_000, 1, high, 0.5, 1) for i, high in enumerate([1, 2, 5, 2, 1])]
    for item in rows[:-1]:
        assert engine.add(item) == []
    levels = engine.add(rows[-1])
    assert len(levels) == 1
    assert levels[0].side is LevelSide.HIGH
    assert levels[0].confirmed_at_ms == rows[-1].closed_at_ms
    assert levels[0].origin_at_ms == rows[2].opened_at_ms


def test_early_lane_emits_once_with_trend_flow_book_and_target() -> None:
    evaluator = StrategyEvaluator(min_trend_move_pct=1, min_book_imbalance=0.01)
    four = [bar(14_400_000, i * 14_400_000, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(7)]
    five = [bar(300_000, i * 300_000, 103, 104, 102, 103.5) for i in range(6)]
    one = [
        bar(60_000, 0, 103, 103.2, 102.9, 103),
        bar(60_000, 60_000, 103, 103.3, 102.9, 103.1),
        bar(60_000, 120_000, 103.1, 103.4, 103, 103.2),
        bar(60_000, 180_000, 103.2, 104.1, 103.2, 104, 500),
    ]
    target = Level("l1", "XUSDT", "4h", LevelSide.HIGH, 110, 0)
    decisions = evaluator.evaluate(
        symbol="XUSDT", now_ms=200_000, bars={"1m": one, "5m": five, "4h": four}, levels=[target], book_imbalance=0.2
    )
    early = decisions[0]
    assert early.status == "TRIGGERED"
    assert early.signal and early.signal.side is Side.LONG
    repeated = evaluator.evaluate(
        symbol="XUSDT", now_ms=201_000, bars={"1m": one, "5m": five, "4h": four}, levels=[target], book_imbalance=0.2
    )
    assert repeated[0].status == "MISSED"


def test_rejects_without_orderbook_confirmation() -> None:
    evaluator = StrategyEvaluator(min_trend_move_pct=1)
    four = [bar(14_400_000, i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(7)]
    result = evaluator.evaluate(
        symbol="XUSDT", now_ms=1, bars={"1m": [], "5m": [], "4h": four}, levels=[], book_imbalance=0
    )
    assert result[0].reason == "warmup"


def test_previous_day_levels_are_not_known_before_day_boundary() -> None:
    day = 86_400_000
    bars = [bar(900_000, day + i * 900_000, 10, 12 + i, 8 - i / 10, 10) for i in range(96)]
    levels = previous_utc_day_levels("XUSDT", bars, 2 * day)
    assert {level.level_class for level in levels} == {"previous_day"}
    assert {level.side for level in levels} == {LevelSide.HIGH, LevelSide.LOW}
    assert all(level.confirmed_at_ms == 2 * day for level in levels)
    assert {level.origin_at_ms for level in levels} == {bars[-1].opened_at_ms}


def test_display_levels_keep_4h_and_clustered_15m_not_single_pivots() -> None:
    levels = [
        Level("h4", "XUSDT", "4h", LevelSide.HIGH, 110, 1),
        Level("a", "XUSDT", "15m", LevelSide.HIGH, 105.00, 1, origin_at_ms=10),
        Level("b", "XUSDT", "15m", LevelSide.HIGH, 105.05, 2, origin_at_ms=20),
        Level("noise", "XUSDT", "15m", LevelSide.LOW, 97, 3),
    ]
    result = current_display_levels(levels, "XUSDT", 100, 10)
    assert {level.level_class for level in result} == {"swing", "cluster"}
    assert all(level.level_id != "noise" for level in result)
    assert next(level for level in result if level.level_class == "cluster").origin_at_ms == 10


def test_wick_and_single_fast_close_do_not_break_level() -> None:
    level = Level("high", "XUSDT", "15m", LevelSide.HIGH, 100.0, 0)
    one = [
        bar(60_000, 0, 99.8, 100.5, 99.7, 99.9),
        bar(60_000, 60_000, 99.9, 100.2, 99.8, 100.01),
        bar(60_000, 120_000, 100.01, 100.1, 99.8, 99.95),
    ]
    [result] = apply_level_breaks([level], {"1m": one}, 180_000, tick_size=0.01)
    assert result.broken_at_ms is None


def test_two_fast_closes_break_level_at_second_close_and_never_reactivate() -> None:
    level = Level("high", "XUSDT", "15m", LevelSide.HIGH, 100.0, 0)
    one = [
        bar(60_000, 0, 99.9, 100.2, 99.8, 100.01),
        bar(60_000, 60_000, 100.01, 100.2, 99.9, 100.02),
        bar(60_000, 120_000, 100.02, 100.1, 99.7, 99.9),
    ]
    [broken] = apply_level_breaks([level], {"1m": one}, 180_000, tick_size=0.01)
    assert broken.broken_at_ms == 120_000
    [still_broken] = apply_level_breaks([broken], {"1m": []}, 240_000, tick_size=0.01)
    assert still_broken.broken_at_ms == 120_000
    assert current_display_levels([still_broken], "XUSDT", 99.9, 240_000) == []


def test_one_source_timeframe_close_breaks_levels_symmetrically() -> None:
    high = Level("high", "XUSDT", "15m", LevelSide.HIGH, 100.0, 0)
    low = Level("low", "XUSDT", "15m", LevelSide.LOW, 90.0, 0)
    fifteen = [bar(900_000, 0, 95, 101, 89, 100.01), bar(900_000, 900_000, 95, 96, 89, 89.99)]
    high_result, low_result = apply_level_breaks(
        [high, low], {"15m": fifteen}, 1_800_000, tick_size=0.01
    )
    assert high_result.broken_at_ms == 900_000
    assert low_result.broken_at_ms == 1_800_000


def test_break_detection_ignores_preconfirmation_and_future_bars() -> None:
    level = Level("high", "XUSDT", "15m", LevelSide.HIGH, 100.0, 120_000)
    one = [
        bar(60_000, 0, 100, 101, 99, 100.1),
        bar(60_000, 60_000, 100, 101, 99, 100.1),
        bar(60_000, 120_000, 100, 101, 99, 100.1),
        bar(60_000, 180_000, 100, 101, 99, 100.1),
    ]
    [before_future_close] = apply_level_breaks([level], {"1m": one}, 239_999, tick_size=0.01)
    assert before_future_close.broken_at_ms is None
    [after_future_close] = apply_level_breaks([level], {"1m": one}, 240_000, tick_size=0.01)
    assert after_future_close.broken_at_ms == 240_000


def test_fast_closes_separated_by_data_gap_are_not_consecutive() -> None:
    level = Level("high", "XUSDT", "15m", LevelSide.HIGH, 100.0, 0)
    one = [
        bar(60_000, 0, 100, 101, 99, 100.1),
        bar(60_000, 120_000, 100, 101, 99, 100.1),
    ]
    [result] = apply_level_breaks([level], {"1m": one}, 180_000, tick_size=0.01)
    assert result.broken_at_ms is None
