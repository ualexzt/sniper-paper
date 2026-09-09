"""Causal horizontal-level strategy candidates for the two frozen lanes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from .levels import (
    Level,
    LevelSide,
    canonical_level_catalog,
    canonical_level_view,
    cluster_levels,
    level_active_at,
)
from .market import Bar
from .paper import PaperSignal, Side


@dataclass(frozen=True)
class StrategyDecision:
    signal: PaperSignal | None
    lane: str
    status: str
    reason: str
    target: Level | None
    features: Mapping[str, float | str]


class CausalLevelEngine:
    """Confirms swing levels only after the configured right-hand bars close."""

    def __init__(self, timeframe: str, left: int = 2, right: int = 2) -> None:
        self.timeframe = timeframe
        self.left = left
        self.right = right
        self._bars: dict[str, list[Bar]] = {}
        self._emitted: set[str] = set()

    def add(self, bar: Bar) -> list[Level]:
        bars = self._bars.setdefault(bar.symbol, [])
        if bars and bar.opened_at_ms <= bars[-1].opened_at_ms:
            raise ValueError("bars must be completed in causal order")
        bars.append(bar)
        window = self.left + self.right + 1
        if len(bars) < window:
            return []
        pivot_index = len(bars) - self.right - 1
        pivot = bars[pivot_index]
        neighbours = bars[pivot_index - self.left : pivot_index] + bars[pivot_index + 1 : pivot_index + self.right + 1]
        result: list[Level] = []
        if all(pivot.high > item.high for item in neighbours):
            result.append(self._level(pivot, LevelSide.HIGH, pivot.high, bar.closed_at_ms))
        if all(pivot.low < item.low for item in neighbours):
            result.append(self._level(pivot, LevelSide.LOW, pivot.low, bar.closed_at_ms))
        fresh = [level for level in result if level.level_id not in self._emitted]
        self._emitted.update(level.level_id for level in fresh)
        return fresh

    def _level(self, bar: Bar, side: LevelSide, price: float, confirmed_at_ms: int) -> Level:
        raw = f"{bar.symbol}:{self.timeframe}:{side.value}:{bar.opened_at_ms}:{price:.12g}"
        level_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
        return Level(
            level_id,
            bar.symbol,
            self.timeframe,
            side,
            price,
            confirmed_at_ms,
            origin_at_ms=bar.opened_at_ms,
        )


class StrategyEvaluator:
    def __init__(
        self,
        *,
        trend_lookback_4h: int = 6,
        min_trend_move_pct: float = 5.0,
        consolidation_bars_5m: int = 6,
        max_consolidation_width_bp: float = 250.0,
        trigger_lookback_1m: int = 3,
        min_book_imbalance: float = 0.05,
        min_reward_risk: float = 1.5,
        terminal_reward_risk: float = 2.0,
        stop_buffer_bp: float = 5.0,
    ) -> None:
        self.trend_lookback_4h = trend_lookback_4h
        self.min_trend_move_pct = min_trend_move_pct
        self.consolidation_bars_5m = consolidation_bars_5m
        self.max_consolidation_width_bp = max_consolidation_width_bp
        self.trigger_lookback_1m = trigger_lookback_1m
        self.min_book_imbalance = min_book_imbalance
        self.min_reward_risk = min_reward_risk
        self.terminal_reward_risk = terminal_reward_risk
        self.stop_buffer_bp = stop_buffer_bp
        self._attempted: set[tuple[str, str]] = set()

    def restore_attempts(self, attempts: Iterable[tuple[str, str]]) -> None:
        self._attempted.update(attempts)

    def evaluate(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Iterable[Level],
        book_imbalance: float,
    ) -> list[StrategyDecision]:
        one = bars.get("1m", ())
        five = bars.get("5m", ())
        four = bars.get("4h", ())
        required = self.trend_lookback_4h + 1
        if len(one) < self.trigger_lookback_1m + 1 or len(five) < self.consolidation_bars_5m or len(four) < required:
            return [StrategyDecision(None, "all", "REJECTED", "warmup", None, {})]
        trend_pct = (four[-1].close / four[-required].close - 1.0) * 100
        if abs(trend_pct) < self.min_trend_move_pct:
            return [StrategyDecision(None, "all", "REJECTED", "weak_4h_trend", None, {"trend_pct": trend_pct})]
        side = Side.LONG if trend_pct > 0 else Side.SHORT
        if side is Side.LONG and book_imbalance < self.min_book_imbalance:
            return [
                StrategyDecision(
                    None, "all", "REJECTED", "book_not_confirmed", None, {"book_imbalance": book_imbalance}
                )
            ]
        if side is Side.SHORT and book_imbalance > -self.min_book_imbalance:
            return [
                StrategyDecision(
                    None, "all", "REJECTED", "book_not_confirmed", None, {"book_imbalance": book_imbalance}
                )
            ]

        cons = five[-self.consolidation_bars_5m :]
        cons_high, cons_low = max(bar.high for bar in cons), min(bar.low for bar in cons)
        price = one[-1].close
        width_bp = (cons_high / cons_low - 1.0) * 10_000
        if width_bp > self.max_consolidation_width_bp:
            return [StrategyDecision(None, "all", "REJECTED", "wide_consolidation", None, {"width_bp": width_bp})]
        previous = one[-self.trigger_lookback_1m - 1 : -1]
        flow_ok = one[-1].delta_notional > 0 if side is Side.LONG else one[-1].delta_notional < 0
        local_break = (
            price > max(bar.high for bar in previous) if side is Side.LONG else price < min(bar.low for bar in previous)
        )
        features: dict[str, float | str] = {
            "trend_pct": trend_pct,
            "width_bp": width_bp,
            "book_imbalance": book_imbalance,
        }
        decisions: list[StrategyDecision] = []
        all_levels = list(levels)
        early_target = _nearest_target(symbol, side, price, all_levels, now_ms, timeframe="4h")
        if early_target is None:
            decisions.append(
                StrategyDecision(None, "early_target_hunt", "REJECTED", "no_causal_4h_target", None, features)
            )
        else:
            early_features = {**features, "target_price": early_target.price}
            decisions.append(
                self._early(
                    symbol,
                    now_ms,
                    side,
                    price,
                    cons_low,
                    cons_high,
                    early_target,
                    local_break and flow_ok,
                    early_features,
                )
            )
        terminal_target = _nearest_target(symbol, side, one[-2].close, all_levels, now_ms, timeframe="15m")
        if terminal_target is None:
            decisions.append(
                StrategyDecision(None, "terminal_level_breakout", "REJECTED", "no_causal_15m_target", None, features)
            )
        else:
            crossed = (
                one[-2].close < terminal_target.price <= price
                if side is Side.LONG
                else one[-2].close > terminal_target.price >= price
            )
            terminal_features = {**features, "target_price": terminal_target.price}
            decisions.append(
                self._terminal(
                    symbol,
                    now_ms,
                    side,
                    price,
                    cons_low,
                    cons_high,
                    terminal_target,
                    crossed and flow_ok,
                    terminal_features,
                )
            )
        return decisions

    def _early(
        self,
        symbol: str,
        now_ms: int,
        side: Side,
        price: float,
        low: float,
        high: float,
        target: Level,
        triggered: bool,
        features: Mapping[str, float | str],
    ) -> StrategyDecision:
        lane = "early_target_hunt"
        if not triggered:
            return StrategyDecision(None, lane, "REJECTED", "no_local_break_with_flow", target, features)
        stop = (
            low * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else high * (1 + self.stop_buffer_bp / 10_000)
        )
        risk = abs(price - stop)
        reward = (target.price - price) if side is Side.LONG else (price - target.price)
        if risk <= 0 or reward / risk < self.min_reward_risk:
            return StrategyDecision(
                None,
                lane,
                "REJECTED",
                "insufficient_target_reward",
                target,
                {**features, "reward_risk": reward / risk if risk else 0.0},
            )
        return self._emit(symbol, now_ms, side, lane, price, stop, target.price, target, features)

    def _terminal(
        self,
        symbol: str,
        now_ms: int,
        side: Side,
        price: float,
        low: float,
        high: float,
        target: Level,
        triggered: bool,
        features: Mapping[str, float | str],
    ) -> StrategyDecision:
        lane = "terminal_level_breakout"
        if not triggered:
            return StrategyDecision(None, lane, "REJECTED", "target_not_crossed_with_flow", target, features)
        stop = (
            low * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else high * (1 + self.stop_buffer_bp / 10_000)
        )
        risk = abs(price - stop)
        if risk <= 0:
            return StrategyDecision(None, lane, "REJECTED", "invalid_stop", target, features)
        take = (
            price + self.terminal_reward_risk * risk if side is Side.LONG else price - self.terminal_reward_risk * risk
        )
        return self._emit(symbol, now_ms, side, lane, price, stop, take, target, features)

    def _emit(
        self,
        symbol: str,
        now_ms: int,
        side: Side,
        lane: str,
        price: float,
        stop: float,
        take: float,
        target: Level,
        features: Mapping[str, float | str],
    ) -> StrategyDecision:
        attempt_key = (target.level_id, lane)
        if attempt_key in self._attempted:
            return StrategyDecision(None, lane, "MISSED", "setup_already_attempted", target, features)
        self._attempted.add(attempt_key)
        raw = f"{symbol}:{lane}:{target.level_id}:{now_ms}"
        signal = PaperSignal(hashlib.sha256(raw.encode()).hexdigest()[:24], now_ms, symbol, side, lane, stop, take)
        return StrategyDecision(signal, lane, "TRIGGERED", "frozen_rules_met", target, features)


def _nearest_target(
    symbol: str, side: Side, price: float, levels: Iterable[Level], now_ms: int, *, timeframe: str
) -> Level | None:
    catalog = canonical_level_catalog(levels, symbol, now_ms)
    raw = [
        level
        for level in catalog
        if level.timeframe == timeframe
        and (
            (side is Side.LONG and level.side is LevelSide.HIGH and level.price > price)
            or (side is Side.SHORT and level.side is LevelSide.LOW and level.price < price)
        )
    ]
    return min(raw, key=lambda level: (abs(level.price - price), level.confirmed_at_ms, level.level_id)) if raw else None


def _cluster_targets(levels: Sequence[Level], side: Side, tolerance_bp: float = 10.0) -> list[Level]:
    return cluster_levels(levels, side, tolerance_bp=tolerance_bp)


def current_display_levels(
    levels: Iterable[Level],
    symbol: str,
    price: float,
    now_ms: int,
    limit: int = 8,
) -> list[Level]:
    """Return the same canonical target families consumed by evaluators."""
    return canonical_level_view(levels, symbol, price, now_ms, limit)


def previous_utc_day_levels(symbol: str, bars_15m: Sequence[Bar], now_ms: int) -> list[Level]:
    day_ms = 86_400_000
    today = now_ms // day_ms * day_ms
    previous_start = today - day_ms
    rows = sorted(
        [bar for bar in bars_15m if previous_start <= bar.opened_at_ms < today and bar.closed_at_ms <= today],
        key=lambda bar: bar.opened_at_ms,
    )
    expected = day_ms // (15 * 60_000)
    if (
        len(rows) != expected
        or rows[0].opened_at_ms != previous_start
        or rows[-1].closed_at_ms != today
        or any(current.opened_at_ms != previous.closed_at_ms for previous, current in pairwise(rows))
    ):
        return []
    high = max(bar.high for bar in rows)
    low = min(bar.low for bar in rows)
    high_origin = next(bar.opened_at_ms for bar in rows if bar.high == high)
    low_origin = next(bar.opened_at_ms for bar in rows if bar.low == low)
    return [
        Level(
            hashlib.sha256(f"{symbol}:prevday:high:{previous_start}".encode()).hexdigest()[:20],
            symbol,
            "15m",
            LevelSide.HIGH,
            high,
            today,
            1,
            "previous_day",
            high_origin,
        ),
        Level(
            hashlib.sha256(f"{symbol}:prevday:low:{previous_start}".encode()).hexdigest()[:20],
            symbol,
            "15m",
            LevelSide.LOW,
            low,
            today,
            1,
            "previous_day",
            low_origin,
        ),
    ]


def _level_is_active(level: Level, now_ms: int) -> bool:
    """Keep a level active until a confirmed close-through invalidates it."""
    return level_active_at(level, now_ms)


def apply_level_breaks(
    levels: Iterable[Level],
    bars: Mapping[str, Sequence[Bar]],
    now_ms: int,
    *,
    tick_size: float,
    buffer_ticks: int = 1,
    fast_timeframe: str = "1m",
    fast_confirming_closes: int = 2,
    source_confirming_closes: int = 1,
) -> list[Level]:
    """Annotate each level's first absorbing causal close-through.

    Wicks do not count. A level breaks after consecutive completed fast-TF
    closes, or after one completed close on the level's own timeframe.
    """
    if tick_size <= 0 or buffer_ticks < 1 or fast_confirming_closes < 1 or source_confirming_closes < 1:
        raise ValueError("level-break parameters must be positive")
    buffer = tick_size * buffer_ticks
    result: list[Level] = []
    for level in levels:
        if level.broken_at_ms is not None:
            result.append(level)
            continue
        candidates = [
            broken_at
            for broken_at in (
                _first_close_break(
                    level,
                    bars.get(level.timeframe, ()),
                    now_ms,
                    buffer,
                    source_confirming_closes,
                ),
                _first_close_break(
                    level,
                    bars.get(fast_timeframe, ()),
                    now_ms,
                    buffer,
                    fast_confirming_closes,
                )
                if level.timeframe != fast_timeframe
                else None,
            )
            if broken_at is not None
        ]
        result.append(replace(level, broken_at_ms=min(candidates)) if candidates else level)
    return result


def _first_close_break(
    level: Level,
    bars: Sequence[Bar],
    now_ms: int,
    buffer: float,
    required_consecutive: int,
) -> int | None:
    threshold = level.price + buffer if level.side is LevelSide.HIGH else level.price - buffer
    epsilon = max(abs(threshold) * 1e-12, buffer * 1e-9)
    consecutive = 0
    previous_closed_at_ms: int | None = None
    for bar in bars:
        if bar.symbol != level.symbol or bar.closed_at_ms <= level.confirmed_at_ms or bar.closed_at_ms > now_ms:
            continue
        if previous_closed_at_ms is not None and bar.opened_at_ms != previous_closed_at_ms:
            consecutive = 0
        beyond = (
            bar.close >= threshold - epsilon
            if level.side is LevelSide.HIGH
            else bar.close <= threshold + epsilon
        )
        consecutive = consecutive + 1 if beyond else 0
        previous_closed_at_ms = bar.closed_at_ms
        if consecutive >= required_consecutive:
            return bar.closed_at_ms
    return None
