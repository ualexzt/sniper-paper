"""Causal horizontal-level strategy candidates for the two frozen lanes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .market import Bar
from .paper import PaperSignal, Side


class LevelSide(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass(frozen=True)
class Level:
    level_id: str
    symbol: str
    timeframe: str
    side: LevelSide
    price: float
    confirmed_at_ms: int
    touches: int = 1
    level_class: str = "swing"


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
        return Level(level_id, bar.symbol, self.timeframe, side, price, confirmed_at_ms)


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
    raw = [
        level
        for level in levels
        if level.symbol == symbol
        and level.timeframe == timeframe
        and level.confirmed_at_ms <= now_ms
        and (
            (side is Side.LONG and level.side is LevelSide.HIGH and level.price > price)
            or (side is Side.SHORT and level.side is LevelSide.LOW and level.price < price)
        )
    ]
    eligible = _cluster_targets(raw, side)
    if timeframe == "4h":
        eligible.extend(level for level in raw if level.level_class in {"swing", "previous_day"})
    else:
        eligible.extend(level for level in raw if level.level_class == "previous_day")
    return min(eligible, key=lambda level: abs(level.price - price)) if eligible else None


def _cluster_targets(levels: Sequence[Level], side: Side, tolerance_bp: float = 10.0) -> list[Level]:
    ordered = sorted(levels, key=lambda level: level.price)
    result: list[Level] = []
    used: set[str] = set()
    for level in ordered:
        if level.level_id in used:
            continue
        group = [
            candidate
            for candidate in ordered
            if candidate.level_id not in used and abs(candidate.price / level.price - 1.0) * 10_000 <= tolerance_bp
        ]
        if sum(candidate.touches for candidate in group) < 2:
            continue
        used.update(candidate.level_id for candidate in group)
        ids = ":".join(sorted(candidate.level_id for candidate in group))
        target_price = (
            min(candidate.price for candidate in group)
            if side is Side.LONG
            else max(candidate.price for candidate in group)
        )
        result.append(
            Level(
                hashlib.sha256(f"cluster:{ids}".encode()).hexdigest()[:20],
                level.symbol,
                level.timeframe,
                level.side,
                target_price,
                max(candidate.confirmed_at_ms for candidate in group),
                sum(candidate.touches for candidate in group),
                "cluster",
            )
        )
    return result


def current_display_levels(
    levels: Iterable[Level], symbol: str, price: float, now_ms: int, limit: int = 8
) -> list[Level]:
    """Return the same structural level families that can become strategy targets."""
    raw = [level for level in levels if level.symbol == symbol and level.confirmed_at_ms <= now_ms]
    four_hour = [level for level in raw if level.timeframe == "4h"]
    previous_day = [level for level in raw if level.timeframe == "15m" and level.level_class == "previous_day"]
    fifteen_highs = [level for level in raw if level.timeframe == "15m" and level.side is LevelSide.HIGH]
    fifteen_lows = [level for level in raw if level.timeframe == "15m" and level.side is LevelSide.LOW]
    clustered = _cluster_targets(fifteen_highs, Side.LONG) + _cluster_targets(fifteen_lows, Side.SHORT)
    unique = {level.level_id: level for level in four_hour + previous_day + clustered}
    return sorted(unique.values(), key=lambda level: (abs(level.price - price), -level.confirmed_at_ms))[:limit]


def previous_utc_day_levels(symbol: str, bars_15m: Sequence[Bar], now_ms: int) -> list[Level]:
    day_ms = 86_400_000
    today = now_ms // day_ms * day_ms
    previous_start = today - day_ms
    rows = [bar for bar in bars_15m if previous_start <= bar.opened_at_ms < today and bar.closed_at_ms <= today]
    if not rows:
        return []
    high = max(bar.high for bar in rows)
    low = min(bar.low for bar in rows)
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
        ),
    ]
