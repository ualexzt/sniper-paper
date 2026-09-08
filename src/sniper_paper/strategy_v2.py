"""Frozen v2 paper-strategy foundation.

The module keeps the evaluator causal, stateful, and lane-isolated. It does not
place orders; it only emits deterministic paper signals and explicit terminal
statuses for replay or downstream simulation.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from statistics import median

from .market import Bar
from .paper import Side


class DecisionStatus(str, Enum):
    REJECTED = "REJECTED"
    MISSED = "MISSED"
    TRIGGERED = "TRIGGERED"


class LevelSide(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class LaneName(str, Enum):
    FAILED_SWEEP_RECLAIM = "failed_sweep_reclaim"
    EARLY_TARGET_HUNT = "early_target_hunt"
    TERMINAL_LEVEL_BREAKOUT = "terminal_level_breakout"
    TARGET_SEEKING_BREAKOUT = "target_seeking_breakout"
    STRUCTURAL_REACTION = "structural_reaction"
    DOM_CONFIRMED_BREAKOUT = "dom_confirmed_breakout"
    CASCADE_IMPULSE = "cascade_impulse"
    FRESH_EXTREME_MOMENTUM = "fresh_extreme_momentum"
    DIAGONAL_CONTEXT = "diagonal_context"


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
    origin_at_ms: int | None = None
    broken_at_ms: int | None = None


@dataclass(frozen=True)
class OrderflowFrame:
    symbol: str
    received_at_ms: int
    best_bid: float
    best_bid_size: float
    best_ask: float
    best_ask_size: float
    delta_notional: float
    median_abs_delta_20: float
    range_bp: float
    median_range_bp_20: float
    top5_bid_notional: float
    top5_ask_notional: float
    atr_1m: float
    book_age_ms: int
    spread_bp: float
    sweep_extreme_price: float | None = None
    sweep_reference_price: float | None = None
    structure_level_price: float | None = None
    diagonal_slope_bp: float = 0.0
    cascade_body_bp: float = 0.0
    cascade_range_bp: float = 0.0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.received_at_ms < 0:
            raise ValueError("received_at_ms must be non-negative")
        if not math.isfinite(self.best_bid) or not math.isfinite(self.best_ask):
            raise ValueError("best prices must be finite")
        if self.best_bid <= 0 or self.best_ask <= 0 or self.best_bid >= self.best_ask:
            raise ValueError("best bid must be positive and below best ask")
        if self.best_bid_size <= 0 or self.best_ask_size <= 0:
            raise ValueError("top-of-book sizes must be positive")
        for value_name in (
            "delta_notional",
            "median_abs_delta_20",
            "range_bp",
            "median_range_bp_20",
            "top5_bid_notional",
            "top5_ask_notional",
            "atr_1m",
            "spread_bp",
            "diagonal_slope_bp",
            "cascade_body_bp",
            "cascade_range_bp",
        ):
            if not math.isfinite(getattr(self, value_name)):
                raise ValueError(f"{value_name} must be finite")
        if self.book_age_ms < 0:
            raise ValueError("book_age_ms must be non-negative")
        if self.spread_bp < 0:
            raise ValueError("spread_bp must be non-negative")
        if self.median_abs_delta_20 < 0 or self.median_range_bp_20 < 0:
            raise ValueError("historical medians must be non-negative")

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def microprice(self) -> float:
        total = self.best_bid_size + self.best_ask_size
        if total <= 0:
            return self.mid
        return (self.best_ask * self.best_bid_size + self.best_bid * self.best_ask_size) / total

    @property
    def book_imbalance(self) -> float:
        total = self.top5_bid_notional + self.top5_ask_notional
        if total <= 0:
            return 0.0
        return (self.top5_bid_notional - self.top5_ask_notional) / total

    @property
    def microprice_mid_bp(self) -> float:
        return (self.microprice / self.mid - 1.0) * 10_000

    @property
    def delta_ratio(self) -> float:
        if self.median_abs_delta_20 <= 0:
            return 0.0
        return abs(self.delta_notional) / self.median_abs_delta_20

    @property
    def body_to_range(self) -> float:
        if self.cascade_range_bp <= 0:
            return 0.0
        return abs(self.cascade_body_bp) / self.cascade_range_bp

    @property
    def sweep_depth_bp(self) -> float | None:
        if self.sweep_reference_price is None or self.sweep_extreme_price is None:
            return None
        if self.sweep_reference_price <= 0:
            return None
        return abs(self.sweep_reference_price / self.sweep_extreme_price - 1.0) * 10_000


@dataclass(frozen=True)
class StrategySignal:
    signal_id: str
    occurred_at_ms: int
    symbol: str
    side: Side
    lane: str
    stop_price: float
    target_price: float
    valid_until_ms: int
    cooldown_until_ms: int


@dataclass(frozen=True)
class StrategyDecision:
    signal: StrategySignal | None
    lane: str
    status: str
    reason: str
    side: Side | None
    target_level: Level | None
    target_price: float | None
    stop_price: float | None
    invalidation_price: float | None
    setup_id: str | None
    features: Mapping[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class _LaneCandidate:
    setup_id: str
    lane: str
    symbol: str
    side: Side
    trigger_now: bool
    armed_at_ms: int
    expires_at_ms: int
    target_level: Level | None
    target_price: float
    stop_price: float
    invalidation_price: float
    reference_price: float | None
    sweep_extreme_price: float | None
    score: float
    features: Mapping[str, float | str]


@dataclass
class _PendingSetup:
    setup_id: str
    lane: str
    symbol: str
    side: Side
    armed_at_ms: int
    expires_at_ms: int
    target_level: Level | None
    target_price: float
    stop_price: float
    invalidation_price: float
    reference_price: float | None
    sweep_extreme_price: float | None
    score: float
    features: Mapping[str, float | str]


def _hash(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _completed(bars: Sequence[Bar], now_ms: int) -> list[Bar]:
    return sorted((bar for bar in bars if bar.closed_at_ms <= now_ms), key=lambda bar: bar.opened_at_ms)


def _last_complete(bars: Sequence[Bar], now_ms: int) -> Bar | None:
    completed = _completed(bars, now_ms)
    return completed[-1] if completed else None


def _true_range(current: Bar, previous_close: float) -> float:
    return max(
        current.high - current.low,
        abs(current.high - previous_close),
        abs(current.low - previous_close),
    )


def _atr_1m(bars_1m: Sequence[Bar]) -> float | None:
    if len(bars_1m) < 15:
        return None
    window = bars_1m[-14:]
    if len(window) < 14:
        return None
    tr_values = []
    for index, current in enumerate(window):
        previous_close = window[index - 1].close if index > 0 else current.close
        tr_values.append(_true_range(current, previous_close))
    return median(tr_values)


def _trend_pct(bars: Sequence[Bar], lookback: int) -> float | None:
    if len(bars) < lookback + 1:
        return None
    base = bars[-lookback - 1].close
    if base <= 0:
        return None
    return (bars[-1].close / base - 1.0) * 100


def _consolidation_width_bp(bars: Sequence[Bar]) -> float:
    high = max(bar.high for bar in bars)
    low = min(bar.low for bar in bars)
    return (high / low - 1.0) * 10_000


def _level_distance_bp(entry: float, target: float) -> float:
    return abs(target / entry - 1.0) * 10_000


def _nearest_target(
    levels: Iterable[Level],
    *,
    symbol: str,
    side: Side,
    price: float,
    now_ms: int,
    timeframes: set[str],
) -> Level | None:
    if side is Side.LONG:
        relevant = [
            level
            for level in levels
            if level.symbol == symbol
            and level.confirmed_at_ms <= now_ms
            and (level.broken_at_ms is None or level.broken_at_ms > now_ms)
            and level.timeframe in timeframes
            and level.side is LevelSide.HIGH
            and level.price > price
        ]
        return min(relevant, key=lambda level: level.price - price) if relevant else None
    relevant = [
        level
        for level in levels
        if level.symbol == symbol
        and level.confirmed_at_ms <= now_ms
        and (level.broken_at_ms is None or level.broken_at_ms > now_ms)
        and level.timeframe in timeframes
        and level.side is LevelSide.LOW
        and level.price < price
    ]
    return min(relevant, key=lambda level: price - level.price) if relevant else None


def _mirror_side(side: Side) -> Side:
    return Side.SHORT if side is Side.LONG else Side.LONG


def _setup_key(symbol: str, lane: str, side: Side) -> str:
    return f"{symbol}:{lane}:{side.value}"


class StrategyV2Evaluator:
    def __init__(
        self,
        *,
        tick_size: float = 0.01,
        entry_latency_ms: int = 250,
        entry_ttl_ms: int = 2_000,
        confirmation_timeout_ms: int = 30_000,
        cooldown_ms: int = 60_000,
        max_book_age_ms: int = 500,
        max_spread_bp: float = 2.0,
        budget_cost_bp: float = 9.5,
        min_risk_bp: float = 10.0,
        max_risk_bp: float = 50.0,
        min_depth_notional_top5: float = 1_000.0,
        trend_lookback_4h: int = 6,
        min_trend_move_pct: float = 5.0,
        consolidation_bars_5m: int = 6,
        max_consolidation_width_bp: float = 250.0,
        trigger_lookback_1m: int = 3,
        min_book_imbalance: float = 0.05,
        min_reward_risk: float = 1.5,
        terminal_reward_risk: float = 2.0,
        stop_buffer_bp: float = 5.0,
        sweep_delta_multiplier: float = 2.0,
        sweep_range_multiplier: float = 0.5,
        structure_reclaim_bp: float = 1.0,
        cascade_min_body_to_range: float = 0.6,
        cascade_min_delta_ratio: float = 2.0,
        fresh_extreme_lookback_4h: int = 6,
    ) -> None:
        self.tick_size = tick_size
        self.entry_latency_ms = entry_latency_ms
        self.entry_ttl_ms = entry_ttl_ms
        self.confirmation_timeout_ms = confirmation_timeout_ms
        self.cooldown_ms = cooldown_ms
        self.max_book_age_ms = max_book_age_ms
        self.max_spread_bp = max_spread_bp
        self.budget_cost_bp = budget_cost_bp
        self.min_risk_bp = min_risk_bp
        self.max_risk_bp = max_risk_bp
        self.min_depth_notional_top5 = min_depth_notional_top5
        self.trend_lookback_4h = trend_lookback_4h
        self.min_trend_move_pct = min_trend_move_pct
        self.consolidation_bars_5m = consolidation_bars_5m
        self.max_consolidation_width_bp = max_consolidation_width_bp
        self.trigger_lookback_1m = trigger_lookback_1m
        self.min_book_imbalance = min_book_imbalance
        self.min_reward_risk = min_reward_risk
        self.terminal_reward_risk = terminal_reward_risk
        self.stop_buffer_bp = stop_buffer_bp
        self.sweep_delta_multiplier = sweep_delta_multiplier
        self.sweep_range_multiplier = sweep_range_multiplier
        self.structure_reclaim_bp = structure_reclaim_bp
        self.cascade_min_body_to_range = cascade_min_body_to_range
        self.cascade_min_delta_ratio = cascade_min_delta_ratio
        self.fresh_extreme_lookback_4h = fresh_extreme_lookback_4h
        self._pending: dict[str, _PendingSetup] = {}
        self._attempted: set[str] = set()
        self._cooldown_until_ms: dict[str, int] = {}
        self._last_triggered: dict[tuple[str, str], tuple[int, str, Side]] = {}

    def restore_attempts(self, attempts: Iterable[str]) -> None:
        self._attempted.update(attempts)

    def evaluate(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Iterable[Level],
        orderflow: OrderflowFrame,
    ) -> list[StrategyDecision]:
        completed = {timeframe: _completed(sequence, now_ms) for timeframe, sequence in bars.items()}
        level_list = [
            level
            for level in levels
            if level.symbol == symbol
            and level.confirmed_at_ms <= now_ms
            and (level.broken_at_ms is None or level.broken_at_ms > now_ms)
        ]
        if orderflow.symbol != symbol or orderflow.received_at_ms > now_ms:
            return [
                self._reject(
                    lane=lane.value,
                    reason="warmup",
                    side=None,
                    features={},
                )
                for lane in LaneName
            ]

        decisions = [
            self._failed_sweep_reclaim(symbol, now_ms, completed, level_list, orderflow),
            self._early_target_hunt(symbol, now_ms, completed, level_list, orderflow),
            self._terminal_level_breakout(symbol, now_ms, completed, level_list, orderflow),
            self._target_seeking_breakout(symbol, now_ms, completed, level_list, orderflow),
            self._structural_reaction(symbol, now_ms, completed, level_list, orderflow),
            self._dom_confirmed_breakout(symbol, now_ms, completed, level_list, orderflow),
            self._cascade_impulse(symbol, now_ms, completed, level_list, orderflow),
            self._fresh_extreme_momentum(symbol, now_ms, completed, level_list, orderflow),
            self._diagonal_context(symbol, now_ms, completed, level_list, orderflow),
        ]
        return decisions

    def _failed_sweep_reclaim(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.FAILED_SWEEP_RECLAIM.value
        repeated = self._last_triggered.get((symbol, lane))
        if repeated is not None and repeated[0] == frame.received_at_ms:
            return self._miss(
                lane=lane,
                reason="setup_already_attempted",
                side=repeated[2],
                setup_id=repeated[1],
                features={},
            )
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        bars_15s = bars.get("15s", ())
        bars_1m = bars.get("1m", ())
        if len(bars_15s) < 21 or len(bars_1m) < 15:
            return self._reject(lane=lane, reason="warmup", side=None, features={})

        candidates = [candidate for candidate in (self._failed_sweep_candidate(symbol, now_ms, bars_15s, bars_1m, levels, frame, side)
                                                  for side in (Side.LONG, Side.SHORT)) if candidate is not None]
        if candidates:
            candidate = max(candidates, key=lambda item: item.score)
            return self._resolve_candidate(candidate, frame)

        for side in (Side.LONG, Side.SHORT):
            pending = self._pending.get(_setup_key(symbol, lane, side))
            if pending is not None:
                confirmation = self._failed_sweep_confirmation_candidate(symbol, bars_15s, bars_1m, frame, pending)
                if confirmation is not None:
                    return self._resolve_candidate(confirmation, frame)
                return self._resolve_pending(pending, frame, None)

        return self._reject(lane=lane, reason="no_sweep_setup", side=None, features={})

    def _failed_sweep_candidate(
        self,
        symbol: str,
        now_ms: int,
        bars_15s: Sequence[Bar],
        bars_1m: Sequence[Bar],
        levels: Sequence[Level],
        frame: OrderflowFrame,
        side: Side,
    ) -> _LaneCandidate | None:
        lane = LaneName.FAILED_SWEEP_RECLAIM.value
        latest = bars_15s[-1]
        prev = bars_15s[-2]
        prior_20 = bars_15s[-21:-1]
        if len(prior_20) < 20:
            return None
        atr = _atr_1m(bars_1m)
        if atr is None or frame.median_abs_delta_20 <= 0 or frame.median_range_bp_20 <= 0:
            return None
        if side is Side.LONG:
            levels_for_side = [
                level
                for level in levels
                if level.side is LevelSide.LOW and level.price <= latest.close and level.confirmed_at_ms <= latest.closed_at_ms
            ]
            if not levels_for_side:
                return None
            level = max(levels_for_side, key=lambda item: item.price)
            if not (prev.close >= level.price and latest.low <= level.price - self.tick_size):
                return None
            if latest.low < level.price - atr:
                return None
            if frame.delta_notional > -self.sweep_delta_multiplier * frame.median_abs_delta_20:
                return None
            if frame.range_bp > self.sweep_range_multiplier * frame.median_range_bp_20:
                return None
            if frame.book_imbalance < self.min_book_imbalance:
                return None
            if frame.top5_bid_notional < self.min_depth_notional_top5:
                return None
            entry = frame.best_bid
            stop = latest.low - max(2 * self.tick_size, 0.2 * atr)
            risk_bp = _level_distance_bp(entry, stop)
            if not (self.min_risk_bp <= risk_bp <= self.max_risk_bp):
                return None
            reward_bp = 2 * risk_bp + 3 * self.budget_cost_bp
            opposite = _nearest_target(levels, symbol=symbol, side=side, price=entry, now_ms=now_ms, timeframes={"4h", "15m"})
            if opposite is not None and _level_distance_bp(entry, opposite.price) < reward_bp:
                return None
            target_price = entry * (1.0 + reward_bp / 10_000)
            invalidation = level.price - self.tick_size
            setup_id = _hash(symbol, lane, side.value, level.level_id, latest.opened_at_ms)
            features = {
                "sweep_depth_bp": float((level.price / latest.low - 1.0) * 10_000),
                "delta_ratio": frame.delta_ratio,
                "range_bp": frame.range_bp,
                "median_range_bp_20": frame.median_range_bp_20,
                "book_imbalance": frame.book_imbalance,
                "atr_1m": atr,
                "reward_bp": reward_bp,
                "risk_bp": risk_bp,
            }
            return _LaneCandidate(
                setup_id=setup_id,
                lane=lane,
                symbol=symbol,
                side=side,
                trigger_now=False,
                armed_at_ms=latest.closed_at_ms,
                expires_at_ms=latest.closed_at_ms + self.confirmation_timeout_ms,
                target_level=opposite,
                target_price=target_price,
                stop_price=stop,
                invalidation_price=invalidation,
                reference_price=level.price,
                sweep_extreme_price=latest.low,
                score=features["sweep_depth_bp"] + frame.delta_ratio,
                features=features,
            )

        levels_for_side = [
            level
            for level in levels
            if level.side is LevelSide.HIGH and level.price >= latest.close and level.confirmed_at_ms <= latest.closed_at_ms
        ]
        if not levels_for_side:
            return None
        level = min(levels_for_side, key=lambda item: item.price)
        if not (prev.close <= level.price and latest.high >= level.price + self.tick_size):
            return None
        if latest.high > level.price + atr:
            return None
        if frame.delta_notional < self.sweep_delta_multiplier * frame.median_abs_delta_20:
            return None
        if frame.range_bp > self.sweep_range_multiplier * frame.median_range_bp_20:
            return None
        if frame.book_imbalance > -self.min_book_imbalance:
            return None
        if frame.top5_ask_notional < self.min_depth_notional_top5:
            return None
        entry = frame.best_ask
        stop = latest.high + max(2 * self.tick_size, 0.2 * atr)
        risk_bp = _level_distance_bp(entry, stop)
        if not (self.min_risk_bp <= risk_bp <= self.max_risk_bp):
            return None
        reward_bp = 2 * risk_bp + 3 * self.budget_cost_bp
        opposite = _nearest_target(levels, symbol=symbol, side=side, price=entry, now_ms=now_ms, timeframes={"4h", "15m"})
        if opposite is not None and _level_distance_bp(entry, opposite.price) < reward_bp:
            return None
        target_price = entry * (1.0 - reward_bp / 10_000)
        invalidation = level.price + self.tick_size
        setup_id = _hash(symbol, lane, side.value, level.level_id, latest.opened_at_ms)
        features = {
            "sweep_depth_bp": float((latest.high / level.price - 1.0) * 10_000),
            "delta_ratio": frame.delta_ratio,
            "range_bp": frame.range_bp,
            "median_range_bp_20": frame.median_range_bp_20,
            "book_imbalance": frame.book_imbalance,
            "atr_1m": atr,
            "reward_bp": reward_bp,
            "risk_bp": risk_bp,
        }
        return _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=False,
            armed_at_ms=latest.closed_at_ms,
            expires_at_ms=latest.closed_at_ms + self.confirmation_timeout_ms,
            target_level=opposite,
            target_price=target_price,
            stop_price=stop,
            invalidation_price=invalidation,
            reference_price=level.price,
            sweep_extreme_price=latest.high,
            score=features["sweep_depth_bp"] + frame.delta_ratio,
            features=features,
        )

    def _failed_sweep_confirmation_candidate(
        self,
        symbol: str,
        bars_15s: Sequence[Bar],
        bars_1m: Sequence[Bar],
        frame: OrderflowFrame,
        pending: _PendingSetup,
    ) -> _LaneCandidate | None:
        if pending.reference_price is None or pending.sweep_extreme_price is None:
            return None
        if len(bars_15s) < 2:
            return None
        latest = bars_15s[-1]
        if latest.closed_at_ms <= pending.armed_at_ms:
            return None
        post_armed = [bar for bar in bars_15s if bar.closed_at_ms > pending.armed_at_ms]
        if not post_armed:
            return None
        if pending.side is Side.LONG:
            if min(bar.low for bar in post_armed) < pending.sweep_extreme_price:
                return None
            if frame.delta_notional < frame.median_abs_delta_20:
                return None
            if frame.best_bid < pending.reference_price + self.tick_size:
                return None
            if frame.microprice <= frame.mid:
                return None
            if frame.book_imbalance < self.min_book_imbalance:
                return None
            if frame.top5_bid_notional < self.min_depth_notional_top5:
                return None
        else:
            if max(bar.high for bar in post_armed) > pending.sweep_extreme_price:
                return None
            if frame.delta_notional > -frame.median_abs_delta_20:
                return None
            if frame.best_ask > pending.reference_price - self.tick_size:
                return None
            if frame.microprice >= frame.mid:
                return None
            if frame.book_imbalance > -self.min_book_imbalance:
                return None
            if frame.top5_ask_notional < self.min_depth_notional_top5:
                return None
        setup_id = pending.setup_id
        features = dict(pending.features)
        features.update(
            {
                "confirmation_delta": frame.delta_notional,
                "microprice_mid_bp": frame.microprice_mid_bp,
                "book_imbalance": frame.book_imbalance,
            }
        )
        return _LaneCandidate(
            setup_id=setup_id,
            lane=pending.lane,
            symbol=symbol,
            side=pending.side,
            trigger_now=True,
            armed_at_ms=latest.closed_at_ms,
            expires_at_ms=pending.expires_at_ms,
            target_level=pending.target_level,
            target_price=pending.target_price,
            stop_price=pending.stop_price,
            invalidation_price=pending.invalidation_price,
            reference_price=pending.reference_price,
            sweep_extreme_price=pending.sweep_extreme_price,
            score=pending.score + frame.delta_ratio,
            features=features,
        )

    def _early_target_hunt(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.EARLY_TARGET_HUNT.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        four = bars.get("4h", ())
        five = bars.get("5m", ())
        one = bars.get("1m", ())
        if len(four) < self.trend_lookback_4h + 1 or len(five) < self.consolidation_bars_5m or len(one) < 4:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        trend = _trend_pct(four, self.trend_lookback_4h)
        if trend is None or abs(trend) < self.min_trend_move_pct:
            return self._reject(lane=lane, reason="weak_trend", side=None, features={"trend_pct": trend or 0.0})
        side = Side.LONG if trend > 0 else Side.SHORT
        current = one[-1]
        previous = one[-self.trigger_lookback_1m - 1 : -1]
        if len(previous) < self.trigger_lookback_1m:
            return self._reject(lane=lane, reason="warmup", side=side, features={"trend_pct": trend})
        cons = five[-self.consolidation_bars_5m :]
        width_bp = _consolidation_width_bp(cons)
        if width_bp > self.max_consolidation_width_bp:
            return self._reject(lane=lane, reason="wide_consolidation", side=side, features={"width_bp": width_bp})
        flow_ok = frame.delta_notional > 0 if side is Side.LONG else frame.delta_notional < 0
        break_ok = current.close > max(item.high for item in previous) if side is Side.LONG else current.close < min(item.low for item in previous)
        book_ok = frame.book_imbalance >= self.min_book_imbalance if side is Side.LONG else frame.book_imbalance <= -self.min_book_imbalance
        target_level = _nearest_target(levels, symbol=symbol, side=side, price=current.close, now_ms=now_ms, timeframes={"4h", "15m"})
        if target_level is None:
            return self._reject(
                lane=lane,
                reason="no_causal_target",
                side=side,
                features={
                    "trend_pct": trend,
                    "width_bp": width_bp,
                    "book_imbalance": frame.book_imbalance,
                },
            )
        stop = min(item.low for item in cons) * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else max(item.high for item in cons) * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        reward_bp = _level_distance_bp(entry, target_level.price)
        if not (self.min_risk_bp <= risk_bp <= self.max_risk_bp):
            return self._reject(lane=lane, reason="risk_out_of_bounds", side=side, features={"risk_bp": risk_bp})
        if reward_bp / risk_bp < self.min_reward_risk:
            return self._reject(
                lane=lane,
                reason="insufficient_target_reward",
                side=side,
                target_level=target_level,
                target_price=target_level.price,
                stop_price=stop,
                invalidation_price=stop,
                features={"reward_risk": reward_bp / risk_bp, "risk_bp": risk_bp, "reward_bp": reward_bp},
            )
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        if flow_ok and break_ok and book_ok:
            return self._reject(
                lane=lane,
                reason="breakout_delegated",
                side=side,
                target_level=target_level,
                target_price=target_level.price,
                stop_price=stop,
                invalidation_price=stop,
                setup_id=setup_id,
                features={
                    "trend_pct": trend,
                    "width_bp": width_bp,
                    "book_imbalance": frame.book_imbalance,
                    "reward_risk": reward_bp / risk_bp,
                    "risk_bp": risk_bp,
                    "reward_bp": reward_bp,
                },
            )
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=False,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.confirmation_timeout_ms,
            target_level=target_level,
            target_price=target_level.price,
            stop_price=stop,
            invalidation_price=stop,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9),
            features={
                "trend_pct": trend,
                "width_bp": width_bp,
                "book_imbalance": frame.book_imbalance,
                "reward_risk": reward_bp / risk_bp,
                "risk_bp": risk_bp,
                "reward_bp": reward_bp,
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _target_seeking_breakout(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.TARGET_SEEKING_BREAKOUT.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        five = bars.get("5m", ())
        one = bars.get("1m", ())
        four = bars.get("4h", ())
        if len(four) < self.trend_lookback_4h + 1 or len(five) < self.consolidation_bars_5m or len(one) < 4:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        trend = _trend_pct(four, self.trend_lookback_4h)
        if trend is None or abs(trend) < self.min_trend_move_pct:
            return self._reject(lane=lane, reason="weak_trend", side=None, features={"trend_pct": trend or 0.0})
        side = Side.LONG if trend > 0 else Side.SHORT
        current = one[-1]
        previous = one[-self.trigger_lookback_1m - 1 : -1]
        if len(previous) < self.trigger_lookback_1m:
            return self._reject(lane=lane, reason="warmup", side=side, features={"trend_pct": trend})
        cons = five[-self.consolidation_bars_5m :]
        width_bp = _consolidation_width_bp(cons)
        if width_bp > self.max_consolidation_width_bp:
            return self._reject(lane=lane, reason="wide_consolidation", side=side, features={"width_bp": width_bp})
        flow_ok = frame.delta_notional > 0 if side is Side.LONG else frame.delta_notional < 0
        break_ok = current.close > max(item.high for item in previous) if side is Side.LONG else current.close < min(item.low for item in previous)
        book_ok = frame.book_imbalance >= self.min_book_imbalance if side is Side.LONG else frame.book_imbalance <= -self.min_book_imbalance
        if not (flow_ok and break_ok and book_ok):
            return self._reject(
                lane=lane,
                reason="no_local_break_with_flow",
                side=side,
                features={
                    "trend_pct": trend,
                    "width_bp": width_bp,
                    "book_imbalance": frame.book_imbalance,
                },
            )
        target_level = _nearest_target(levels, symbol=symbol, side=side, price=current.close, now_ms=now_ms, timeframes={"4h", "15m"})
        if target_level is None:
            return self._reject(
                lane=lane,
                reason="no_causal_target",
                side=side,
                features={
                    "trend_pct": trend,
                    "width_bp": width_bp,
                    "book_imbalance": frame.book_imbalance,
                },
            )
        stop = min(item.low for item in cons) * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else max(item.high for item in cons) * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        reward_bp = _level_distance_bp(entry, target_level.price)
        if not (self.min_risk_bp <= risk_bp <= self.max_risk_bp):
            return self._reject(lane=lane, reason="risk_out_of_bounds", side=side, features={"risk_bp": risk_bp})
        if reward_bp / risk_bp < self.min_reward_risk:
            return self._reject(
                lane=lane,
                reason="insufficient_target_reward",
                side=side,
                target_level=target_level,
                target_price=target_level.price,
                stop_price=stop,
                invalidation_price=stop,
                features={"reward_risk": reward_bp / risk_bp, "risk_bp": risk_bp, "reward_bp": reward_bp},
            )
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=True,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.entry_ttl_ms,
            target_level=target_level,
            target_price=target_level.price,
            stop_price=stop,
            invalidation_price=stop,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9),
            features={
                "trend_pct": trend,
                "width_bp": width_bp,
                "book_imbalance": frame.book_imbalance,
                "reward_risk": reward_bp / risk_bp,
                "risk_bp": risk_bp,
                "reward_bp": reward_bp,
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _terminal_level_breakout(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.TERMINAL_LEVEL_BREAKOUT.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        one = bars.get("1m", ())
        if len(one) < 2:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        current = one[-1]
        previous = one[-2]
        side: Side | None = None
        target_level: Level | None = None
        if frame.delta_notional > 0 and current.close > previous.close:
            target_level = _nearest_target(levels, symbol=symbol, side=Side.LONG, price=previous.close, now_ms=now_ms, timeframes={"15m"})
            if target_level is not None and previous.close < target_level.price <= current.close:
                side = Side.LONG
        elif frame.delta_notional < 0 and current.close < previous.close:
            target_level = _nearest_target(levels, symbol=symbol, side=Side.SHORT, price=previous.close, now_ms=now_ms, timeframes={"15m"})
            if target_level is not None and previous.close > target_level.price >= current.close:
                side = Side.SHORT
        if side is None or target_level is None:
            return self._reject(
                lane=lane,
                reason="target_not_crossed_with_flow",
                side=side,
                features={"delta_notional": frame.delta_notional, "microprice_mid_bp": frame.microprice_mid_bp},
            )
        stop = min(previous.low, current.low) * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else max(previous.high, current.high) * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        if not (self.min_risk_bp <= risk_bp <= self.max_risk_bp):
            return self._reject(lane=lane, reason="invalid_stop", side=side, features={"risk_bp": risk_bp})
        reward_bp = self.terminal_reward_risk * risk_bp
        target_price = entry * (1.0 + reward_bp / 10_000) if side is Side.LONG else entry * (1.0 - reward_bp / 10_000)
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=True,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.entry_ttl_ms,
            target_level=target_level,
            target_price=target_price,
            stop_price=stop,
            invalidation_price=target_level.price,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9),
            features={
                "delta_notional": frame.delta_notional,
                "reward_risk": self.terminal_reward_risk,
                "risk_bp": risk_bp,
                "target_crossed": 1.0,
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _structural_reaction(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.STRUCTURAL_REACTION.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        fifteen = bars.get("15m", ())
        if len(fifteen) < 2:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        current = fifteen[-1]
        previous = fifteen[-2]
        side: Side | None = None
        target_level: Level | None = None
        if current.low <= previous.low and current.close >= previous.close and frame.book_imbalance >= self.min_book_imbalance and frame.microprice > frame.mid:
            target_level = _nearest_target(levels, symbol=symbol, side=Side.LONG, price=current.close, now_ms=now_ms, timeframes={"15m", "4h"})
            if target_level is not None and current.close >= previous.low + self.structure_reclaim_bp * previous.low / 10_000:
                side = Side.LONG
        elif current.high >= previous.high and current.close <= previous.close and frame.book_imbalance <= -self.min_book_imbalance and frame.microprice < frame.mid:
            target_level = _nearest_target(levels, symbol=symbol, side=Side.SHORT, price=current.close, now_ms=now_ms, timeframes={"15m", "4h"})
            if target_level is not None and current.close <= previous.high - self.structure_reclaim_bp * previous.high / 10_000:
                side = Side.SHORT
        if side is None or target_level is None:
            return self._reject(lane=lane, reason="no_structural_reaction", side=side, features={"book_imbalance": frame.book_imbalance})
        stop = current.low * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else current.high * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        reward_bp = _level_distance_bp(entry, target_level.price)
        if reward_bp / max(risk_bp, 1e-9) < self.min_reward_risk:
            return self._reject(
                lane=lane,
                reason="insufficient_target_reward",
                side=side,
                target_level=target_level,
                target_price=target_level.price,
                stop_price=stop,
                invalidation_price=stop,
                features={"reward_risk": reward_bp / max(risk_bp, 1e-9)},
            )
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=True,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.entry_ttl_ms,
            target_level=target_level,
            target_price=target_level.price,
            stop_price=stop,
            invalidation_price=stop,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9),
            features={
                "book_imbalance": frame.book_imbalance,
                "reward_risk": reward_bp / max(risk_bp, 1e-9),
                "microprice_mid_bp": frame.microprice_mid_bp,
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _dom_confirmed_breakout(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        del symbol, now_ms, bars, levels
        lane = LaneName.DOM_CONFIRMED_BREAKOUT.value
        return self._reject(
            lane=lane,
            reason="diagnostic_only",
            side=None,
            features={
                "book_imbalance": frame.book_imbalance,
                "microprice_mid_bp": frame.microprice_mid_bp,
            },
        )

    def _cascade_impulse(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.CASCADE_IMPULSE.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        one = bars.get("1m", ())
        if len(one) < 2:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        current = one[-1]
        previous = one[-2]
        side: Side | None = None
        target_level: Level | None = None
        body_to_range = abs(current.close - current.open) / max(current.high - current.low, 1e-9)
        delta_ratio = frame.delta_ratio
        if current.close > previous.high and frame.delta_notional > 0 and body_to_range >= self.cascade_min_body_to_range and delta_ratio >= self.cascade_min_delta_ratio:
            side = Side.LONG
            target_level = _nearest_target(levels, symbol=symbol, side=Side.LONG, price=current.close, now_ms=now_ms, timeframes={"15m", "4h"})
        elif current.close < previous.low and frame.delta_notional < 0 and body_to_range >= self.cascade_min_body_to_range and delta_ratio >= self.cascade_min_delta_ratio:
            side = Side.SHORT
            target_level = _nearest_target(levels, symbol=symbol, side=Side.SHORT, price=current.close, now_ms=now_ms, timeframes={"15m", "4h"})
        if side is None or target_level is None:
            return self._reject(
                lane=lane,
                reason="no_cascade_impulse",
                side=side,
                features={"body_to_range": body_to_range, "delta_ratio": delta_ratio},
            )
        stop = current.low * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else current.high * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        reward_bp = _level_distance_bp(entry, target_level.price)
        if reward_bp / max(risk_bp, 1e-9) < self.min_reward_risk:
            return self._reject(lane=lane, reason="insufficient_target_reward", side=side, features={"reward_risk": reward_bp / max(risk_bp, 1e-9)})
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=True,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.entry_ttl_ms,
            target_level=target_level,
            target_price=target_level.price,
            stop_price=stop,
            invalidation_price=stop,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9),
            features={
                "body_to_range": body_to_range,
                "delta_ratio": delta_ratio,
                "reward_risk": reward_bp / max(risk_bp, 1e-9),
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _fresh_extreme_momentum(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        lane = LaneName.FRESH_EXTREME_MOMENTUM.value
        if not self._data_quality_ok(frame):
            return self._reject(lane=lane, reason="data_quality", side=None, features={"spread_bp": frame.spread_bp})
        four = bars.get("4h", ())
        one = bars.get("1m", ())
        if len(four) < self.fresh_extreme_lookback_4h + 1 or len(one) < 2:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        current = one[-1]
        lookback = four[-(self.fresh_extreme_lookback_4h + 1) : -1]
        if len(lookback) < self.fresh_extreme_lookback_4h:
            return self._reject(lane=lane, reason="warmup", side=None, features={})
        side: Side | None = None
        target_level: Level | None = None
        if current.close > max(item.high for item in lookback) and frame.delta_notional > 0 and frame.book_imbalance >= self.min_book_imbalance:
            side = Side.LONG
            target_level = _nearest_target(levels, symbol=symbol, side=Side.LONG, price=current.close, now_ms=now_ms, timeframes={"4h"})
        elif current.close < min(item.low for item in lookback) and frame.delta_notional < 0 and frame.book_imbalance <= -self.min_book_imbalance:
            side = Side.SHORT
            target_level = _nearest_target(levels, symbol=symbol, side=Side.SHORT, price=current.close, now_ms=now_ms, timeframes={"4h"})
        if side is None or target_level is None:
            return self._reject(
                lane=lane,
                reason="no_fresh_extreme",
                side=side,
                features={"book_imbalance": frame.book_imbalance, "delta_ratio": frame.delta_ratio},
            )
        stop = current.low * (1 - self.stop_buffer_bp / 10_000) if side is Side.LONG else current.high * (1 + self.stop_buffer_bp / 10_000)
        entry = frame.best_bid if side is Side.LONG else frame.best_ask
        risk_bp = _level_distance_bp(entry, stop)
        reward_bp = _level_distance_bp(entry, target_level.price)
        if reward_bp / max(risk_bp, 1e-9) < self.min_reward_risk:
            return self._reject(lane=lane, reason="insufficient_target_reward", side=side, features={"reward_risk": reward_bp / max(risk_bp, 1e-9)})
        setup_id = _hash(symbol, lane, side.value, target_level.level_id, current.opened_at_ms)
        candidate = _LaneCandidate(
            setup_id=setup_id,
            lane=lane,
            symbol=symbol,
            side=side,
            trigger_now=True,
            armed_at_ms=current.closed_at_ms,
            expires_at_ms=current.closed_at_ms + self.entry_ttl_ms,
            target_level=target_level,
            target_price=target_level.price,
            stop_price=stop,
            invalidation_price=stop,
            reference_price=None,
            sweep_extreme_price=None,
            score=reward_bp / max(risk_bp, 1e-9) + frame.delta_ratio,
            features={
                "book_imbalance": frame.book_imbalance,
                "delta_ratio": frame.delta_ratio,
                "fresh_extreme_lookback": float(self.fresh_extreme_lookback_4h),
            },
        )
        return self._resolve_candidate(candidate, frame)

    def _diagonal_context(
        self,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Sequence[Level],
        frame: OrderflowFrame,
    ) -> StrategyDecision:
        del symbol, now_ms, levels
        lane = LaneName.DIAGONAL_CONTEXT.value
        four = bars.get("4h", ())
        if len(four) < self.trend_lookback_4h + 1:
            return self._reject(lane=lane, reason="diagnostic_only", side=None, features={"book_imbalance": frame.book_imbalance})
        slope = _trend_pct(four, self.trend_lookback_4h) or 0.0
        return self._reject(
            lane=lane,
            reason="diagnostic_only",
            side=None,
            features={
                "diagonal_slope_bp": slope * 100.0,
                "book_imbalance": frame.book_imbalance,
            },
        )

    def _resolve_candidate(self, candidate: _LaneCandidate, frame: OrderflowFrame) -> StrategyDecision:
        key = _setup_key(candidate.symbol, candidate.lane, candidate.side)
        cooldown_until = self._cooldown_until_ms.get(key, 0)
        pending = self._pending.get(key)
        if candidate.setup_id in self._attempted:
            return self._miss(
                lane=candidate.lane,
                reason="setup_already_attempted",
                side=candidate.side,
                target_level=candidate.target_level,
                target_price=candidate.target_price,
                stop_price=candidate.stop_price,
                invalidation_price=candidate.invalidation_price,
                setup_id=candidate.setup_id,
                features=candidate.features,
            )
        if pending is not None and frame.received_at_ms > pending.expires_at_ms:
            self._pending.pop(key, None)
            self._attempted.add(pending.setup_id)
            self._cooldown_until_ms[key] = pending.expires_at_ms + self.cooldown_ms
            if pending.setup_id == candidate.setup_id:
                return self._miss(
                    lane=pending.lane,
                    reason="setup_expired",
                    side=pending.side,
                    target_level=pending.target_level,
                    target_price=pending.target_price,
                    stop_price=pending.stop_price,
                    invalidation_price=pending.invalidation_price,
                    setup_id=pending.setup_id,
                    features=pending.features,
                )
            pending = None
        if pending is not None and pending.setup_id != candidate.setup_id:
            return self._reject(
                lane=candidate.lane,
                reason="lane_busy_with_pending_setup",
                side=candidate.side,
                target_level=pending.target_level,
                target_price=pending.target_price,
                stop_price=pending.stop_price,
                invalidation_price=pending.invalidation_price,
                setup_id=pending.setup_id,
                features=pending.features,
            )
        if cooldown_until and frame.received_at_ms < cooldown_until:
            return self._reject(
                lane=candidate.lane,
                reason="lane_cooldown_active",
                side=candidate.side,
                target_level=candidate.target_level,
                target_price=candidate.target_price,
                stop_price=candidate.stop_price,
                invalidation_price=candidate.invalidation_price,
                setup_id=candidate.setup_id,
                features=candidate.features,
            )
        if pending is not None and pending.setup_id == candidate.setup_id and candidate.trigger_now:
            self._pending.pop(key, None)
            self._attempted.add(candidate.setup_id)
            self._cooldown_until_ms[key] = candidate.armed_at_ms + self.cooldown_ms
            return self._trigger(candidate, frame)
        if candidate.trigger_now:
            self._attempted.add(candidate.setup_id)
            self._cooldown_until_ms[key] = candidate.armed_at_ms + self.cooldown_ms
            return self._trigger(candidate, frame)
        self._pending[key] = _PendingSetup(
            setup_id=candidate.setup_id,
            lane=candidate.lane,
            symbol=candidate.symbol,
            side=candidate.side,
            armed_at_ms=candidate.armed_at_ms,
            expires_at_ms=candidate.expires_at_ms,
            target_level=candidate.target_level,
            target_price=candidate.target_price,
            stop_price=candidate.stop_price,
            invalidation_price=candidate.invalidation_price,
            reference_price=candidate.reference_price,
            sweep_extreme_price=candidate.sweep_extreme_price,
            score=candidate.score,
            features=candidate.features,
        )
        return self._reject(
            lane=candidate.lane,
            reason="armed_pending_confirmation",
            side=candidate.side,
            target_level=candidate.target_level,
            target_price=candidate.target_price,
            stop_price=candidate.stop_price,
            invalidation_price=candidate.invalidation_price,
            setup_id=candidate.setup_id,
            features=candidate.features,
        )

    def _resolve_pending(
        self,
        pending: _PendingSetup,
        frame: OrderflowFrame,
        candidate: _LaneCandidate | None,
    ) -> StrategyDecision:
        key = _setup_key(pending.symbol, pending.lane, pending.side)
        if candidate is not None and candidate.setup_id != pending.setup_id:
            return self._reject(
                lane=pending.lane,
                reason="lane_busy_with_pending_setup",
                side=pending.side,
                target_level=pending.target_level,
                target_price=pending.target_price,
                stop_price=pending.stop_price,
                invalidation_price=pending.invalidation_price,
                setup_id=pending.setup_id,
                features=pending.features,
            )
        if frame.received_at_ms > pending.expires_at_ms:
            self._pending.pop(key, None)
            self._attempted.add(pending.setup_id)
            self._cooldown_until_ms[key] = pending.expires_at_ms + self.cooldown_ms
            return self._miss(
                lane=pending.lane,
                reason="setup_expired",
                side=pending.side,
                target_level=pending.target_level,
                target_price=pending.target_price,
                stop_price=pending.stop_price,
                invalidation_price=pending.invalidation_price,
                setup_id=pending.setup_id,
                features=pending.features,
            )
        if candidate is None:
            return self._reject(
                lane=pending.lane,
                reason="waiting_confirmation",
                side=pending.side,
                target_level=pending.target_level,
                target_price=pending.target_price,
                stop_price=pending.stop_price,
                invalidation_price=pending.invalidation_price,
                setup_id=pending.setup_id,
                features=pending.features,
            )
        self._pending.pop(key, None)
        self._attempted.add(pending.setup_id)
        self._cooldown_until_ms[key] = candidate.armed_at_ms + self.cooldown_ms
        return self._trigger(candidate, frame)

    def _data_quality_ok(self, frame: OrderflowFrame) -> bool:
        return frame.book_age_ms <= self.max_book_age_ms and frame.spread_bp <= self.max_spread_bp

    def _trigger(self, candidate: _LaneCandidate, frame: OrderflowFrame) -> StrategyDecision:
        self._last_triggered[(candidate.symbol, candidate.lane)] = (
            frame.received_at_ms,
            candidate.setup_id,
            candidate.side,
        )
        signal = StrategySignal(
            signal_id=_hash(candidate.symbol, candidate.lane, candidate.setup_id, frame.received_at_ms),
            occurred_at_ms=frame.received_at_ms,
            symbol=candidate.symbol,
            side=candidate.side,
            lane=candidate.lane,
            stop_price=candidate.stop_price,
            target_price=candidate.target_price,
            valid_until_ms=frame.received_at_ms + self.entry_ttl_ms,
            cooldown_until_ms=frame.received_at_ms + self.cooldown_ms,
        )
        features = dict(candidate.features)
        features.update(
            {
                "best_bid": frame.best_bid,
                "best_ask": frame.best_ask,
                "microprice_mid_bp": frame.microprice_mid_bp,
                "setup_score": candidate.score,
            }
        )
        return StrategyDecision(
            signal=signal,
            lane=candidate.lane,
            status=DecisionStatus.TRIGGERED.value,
            reason="frozen_rules_met",
            side=candidate.side,
            target_level=candidate.target_level,
            target_price=candidate.target_price,
            stop_price=candidate.stop_price,
            invalidation_price=candidate.invalidation_price,
            setup_id=candidate.setup_id,
            features=features,
        )

    def _reject(
        self,
        *,
        lane: str,
        reason: str,
        side: Side | None,
        features: Mapping[str, float | str],
        target_level: Level | None = None,
        target_price: float | None = None,
        stop_price: float | None = None,
        invalidation_price: float | None = None,
        setup_id: str | None = None,
    ) -> StrategyDecision:
        return StrategyDecision(
            signal=None,
            lane=lane,
            status=DecisionStatus.REJECTED.value,
            reason=reason,
            side=side,
            target_level=target_level,
            target_price=target_price,
            stop_price=stop_price,
            invalidation_price=invalidation_price,
            setup_id=setup_id,
            features=dict(features),
        )

    def _miss(
        self,
        *,
        lane: str,
        reason: str,
        side: Side | None,
        features: Mapping[str, float | str],
        target_level: Level | None = None,
        target_price: float | None = None,
        stop_price: float | None = None,
        invalidation_price: float | None = None,
        setup_id: str | None = None,
    ) -> StrategyDecision:
        return StrategyDecision(
            signal=None,
            lane=lane,
            status=DecisionStatus.MISSED.value,
            reason=reason,
            side=side,
            target_level=target_level,
            target_price=target_price,
            stop_price=stop_price,
            invalidation_price=invalidation_price,
            setup_id=setup_id,
            features=dict(features),
        )
