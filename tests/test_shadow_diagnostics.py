from __future__ import annotations

import pytest

from sniper_paper.market import Bar
from sniper_paper.paper import Side
from sniper_paper.shadow_orderflow import DensityWallEvidence, ShadowOrderflowEvaluator, ShadowStatus
from sniper_paper.shadow_setups import (
    ShadowSetupPhase,
    ShadowSetupStatus,
    diagnose_retest_reclaim_v1,
)
from sniper_paper.strategy import Level as SetupLevel
from sniper_paper.strategy import LevelSide as SetupLevelSide
from sniper_paper.strategy_v2 import Level as FlowLevel
from sniper_paper.strategy_v2 import LevelSide as FlowLevelSide
from sniper_paper.strategy_v2 import StrategySignal


def bar(
    timeframe_ms: int,
    opened_at_ms: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    delta_notional: float = 1.0,
) -> Bar:
    return Bar(
        "XUSDT",
        timeframe_ms,
        opened_at_ms,
        opened_at_ms + timeframe_ms,
        open_,
        high,
        low,
        close,
        1.0,
        delta_notional,
        1,
    )


def assert_shadow_only(result: object) -> None:
    assert not hasattr(result, "signal")
    assert not isinstance(result, StrategySignal)


def test_retest_reclaim_ignores_future_bars_and_uses_later_reaction_confirmation() -> None:
    setup_level = SetupLevel("breakout", "XUSDT", "15m", SetupLevelSide.HIGH, 100.0, 0)
    target_near = SetupLevel("target_near", "XUSDT", "15m", SetupLevelSide.HIGH, 100.09, 0)
    target_far = SetupLevel("target_far", "XUSDT", "15m", SetupLevelSide.HIGH, 100.15, 0)
    future_target = SetupLevel("future_target", "XUSDT", "15m", SetupLevelSide.HIGH, 100.085, 300_000)
    levels = [setup_level, target_near, target_far, future_target]

    bars_with_future = {
        "1m": [
            bar(60_000, 0, 99.98, 100.03, 99.97, 100.02, 12.0),
            bar(60_000, 60_000, 100.02, 100.05, 99.98, 100.03, 13.0),
            bar(60_000, 120_000, 100.03, 100.06, 99.99, 100.00, -8.0),
            bar(60_000, 180_000, 100.00, 100.10, 99.98, 100.08, 18.0),
            bar(60_000, 240_000, 100.07, 100.20, 100.01, 99.90, -9.0),
        ]
    }
    bars_without_future = {"1m": bars_with_future["1m"][:-1]}

    with_future = diagnose_retest_reclaim_v1(
        symbol="XUSDT",
        now_ms=240_000,
        bars=bars_with_future,
        levels=levels,
        tick_size=0.01,
    )
    without_future = diagnose_retest_reclaim_v1(
        symbol="XUSDT",
        now_ms=240_000,
        bars=bars_without_future,
        levels=levels,
        tick_size=0.01,
    )

    assert with_future == without_future

    setup = next(item for item in with_future if item.level_id == "breakout")
    assert setup.status is ShadowSetupStatus.CONFIRMED
    assert setup.phase is ShadowSetupPhase.RECLAIMED
    assert setup.broken_at_ms == 120_000
    assert setup.retest_at_ms == 180_000
    assert setup.reclaim_at_ms == 240_000
    assert setup.stop_price == pytest.approx(99.98)
    assert setup.target_price == pytest.approx(100.09)
    assert setup.features["target_level_id"] == "target_near"
    assert_shadow_only(setup)


def test_retest_reclaim_cannot_confirm_after_frozen_expiry() -> None:
    level = SetupLevel("breakout", "XUSDT", "15m", SetupLevelSide.HIGH, 100.0, 0, broken_at_ms=60_000)
    bars = {
        "1m": [
            bar(60_000, 60_000, 100.1, 100.2, 99.99, 100.05),
            bar(60_000, 120_000, 100.05, 100.4, 100.0, 100.35),
        ]
    }

    setup = diagnose_retest_reclaim_v1(
        symbol="XUSDT",
        now_ms=180_000,
        bars=bars,
        levels=[level],
        tick_size=0.01,
        max_setup_age_ms=60_000,
    )[0]

    assert setup.status is ShadowSetupStatus.EXPIRED
    assert setup.reclaim_at_ms is None


def test_density_ambiguous_removal_is_blocked_not_a_fill() -> None:
    wall = DensityWallEvidence(
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        observed_at_ms=1_000,
        wall_age_ms=2_000,
        initial_size=100.0,
        current_remaining=0.0,
        source_event_id="evt-removal",
        evidence_quality="ambiguous",
    )

    diag = ShadowOrderflowEvaluator().density_bounce_v1(
        symbol="XUSDT",
        now_ms=20_000,
        bars={"15s": [bar(15_000, 0, 100.0, 100.05, 99.9, 99.95, -12.0)]},
        wall=wall,
        levels=(),
    )

    assert diag.status is ShadowStatus.BLOCKED
    assert diag.reason == "blocked_ambiguous_wall_removal"


def test_density_second_approach_is_not_reused() -> None:
    wall = DensityWallEvidence(
        symbol="XUSDT",
        side=Side.LONG,
        price=100.0,
        observed_at_ms=1_000,
        wall_age_ms=40_000,
        initial_size=100.0,
        current_remaining=90.0,
        source_event_id="evt-first-approach",
    )
    bars = {
        "15s": [
            bar(15_000, 0, 100.1, 100.2, 100.0, 100.1, 5.0),
            bar(15_000, 15_000, 100.1, 100.15, 100.05, 100.1, 4.0),
            bar(15_000, 30_000, 100.1, 100.2, 99.99, 100.12, 6.0),
        ]
    }

    diag = ShadowOrderflowEvaluator().density_bounce_v1(symbol="XUSDT", now_ms=45_000, bars=bars, wall=wall, levels=())

    assert diag.status is ShadowStatus.INVALIDATED
    assert diag.reason == "first_approach_already_consumed"


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    [
        ("wall_age_ms", "blocked_missing_wall_age"),
        ("initial_size", "blocked_missing_initial_size"),
        ("current_remaining", "blocked_missing_current_remaining"),
    ],
)
def test_density_missing_fields_are_blocked(missing_field: str, expected_reason: str) -> None:
    evaluator = ShadowOrderflowEvaluator()
    kwargs = {
        "symbol": "XUSDT",
        "side": Side.LONG,
        "price": 100.0,
        "observed_at_ms": 1_000,
        "wall_age_ms": 2_000,
        "initial_size": 100.0,
        "current_remaining": 80.0,
        "source_event_id": "evt-1",
    }
    kwargs[missing_field] = None
    wall = DensityWallEvidence(**kwargs)

    diag = evaluator.density_bounce_v1(
        symbol="XUSDT",
        now_ms=2_000,
        bars={"15s": [bar(15_000, 0, 100.0, 100.1, 99.9, 100.0, 10.0)]},
        wall=wall,
        levels=(),
    )

    assert diag.status is ShadowStatus.BLOCKED
    assert diag.reason == expected_reason
    assert_shadow_only(diag)


@pytest.mark.parametrize("remaining", [50.0, 40.0])
def test_density_walls_at_or_below_half_remaining_are_invalidated(remaining: float) -> None:
    evaluator = ShadowOrderflowEvaluator()
    wall = DensityWallEvidence(
        symbol="XUSDT",
        side=Side.SHORT,
        price=100.0,
        observed_at_ms=1_000,
        wall_age_ms=2_000,
        initial_size=100.0,
        current_remaining=remaining,
        source_event_id="evt-2",
    )

    diag = evaluator.density_bounce_v1(
        symbol="XUSDT",
        now_ms=20_000,
        bars={"15s": [bar(15_000, 0, 100.0, 100.05, 99.9, 99.95, -12.0)]},
        wall=wall,
        levels=(),
    )

    assert diag.status is ShadowStatus.INVALIDATED
    assert diag.reason == "wall_eroded_to_half"
    assert_shadow_only(diag)


def test_cascade_terminal_exit_uses_levels_causal_before_previous_bar() -> None:
    evaluator = ShadowOrderflowEvaluator()
    bars = {
        "1m": [
            bar(60_000, 0, 100.00, 100.15, 99.95, 100.10, 10.0),
            bar(60_000, 60_000, 100.10, 100.65, 100.05, 100.60, 30.0),
        ]
    }
    levels = [
        FlowLevel("causal", "XUSDT", "15m", FlowLevelSide.HIGH, 100.40, 60_000),
        FlowLevel("future", "XUSDT", "15m", FlowLevelSide.HIGH, 100.41, 60_001),
    ]

    diag = evaluator.cascade_terminal_exit(symbol="XUSDT", now_ms=120_000, bars=bars, levels=levels)

    assert diag.status is ShadowStatus.OBSERVED
    assert diag.reason == "frozen_rules_met"
    assert diag.features["target_level_id"] == "causal"
    assert diag.features["target_level_price"] == pytest.approx(100.40)
    assert_shadow_only(diag)
