"""Shadow-only causal diagnostics for density and cascade hypotheses."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from statistics import median
from types import MappingProxyType

from .market import Bar
from .paper import Side
from .strategy_v2 import Level, LevelSide

__all__ = [
    "ShadowStatus",
    "DensityWallEvidence",
    "ShadowDiagnostic",
    "ShadowOrderflowEvaluator",
]


class ShadowStatus(str, Enum):
    OBSERVED = "OBSERVED"
    ARMED = "ARMED"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class DensityWallEvidence:
    symbol: str
    side: Side
    price: float
    observed_at_ms: int
    wall_age_ms: int | None = None
    initial_size: float | None = None
    current_remaining: float | None = None
    source_event_id: str | None = None
    evidence_quality: str = "observed"

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not isinstance(self.side, Side):
            raise TypeError("side must be a Side")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        if self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be non-negative")
        for name in ("wall_age_ms",):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("initial_size", "current_remaining"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.source_event_id is not None and not self.source_event_id:
            raise ValueError("source_event_id cannot be empty")
        if self.evidence_quality not in {"observed", "ambiguous"}:
            raise ValueError("evidence_quality must be observed or ambiguous")


def _freeze_features(features: Mapping[str, float | str]) -> Mapping[str, float | str]:
    normalized: dict[str, float | str] = {}
    for key, value in features.items():
        if not isinstance(key, str) or not key:
            raise ValueError("feature names must be non-empty strings")
        if isinstance(value, bool):
            raise TypeError(f"feature {key} cannot be boolean")
        if isinstance(value, str):
            normalized[key] = value
            continue
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"feature {key} must be finite")
            normalized[key] = numeric
            continue
        raise TypeError(f"feature {key} must be numeric or text")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class ShadowDiagnostic:
    diagnostic_id: str
    setup_id: str
    occurred_at_ms: int
    symbol: str
    lane: str
    side: Side | None
    status: ShadowStatus
    reason: str
    reference_price: float | None
    stop_price: float | None
    target_price: float | None
    features: Mapping[str, float | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.diagnostic_id:
            raise ValueError("diagnostic_id is required")
        if not self.setup_id:
            raise ValueError("setup_id is required")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.lane:
            raise ValueError("lane is required")
        if self.occurred_at_ms < 0:
            raise ValueError("occurred_at_ms must be non-negative")
        if self.side is not None and not isinstance(self.side, Side):
            raise TypeError("side must be a Side or None")
        if not isinstance(self.status, ShadowStatus):
            raise TypeError("status must be a ShadowStatus")
        if not self.reason:
            raise ValueError("reason is required")
        for name in ("reference_price", "stop_price", "target_price"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        object.__setattr__(self, "features", _freeze_features(self.features))

    def to_record(self, protocol_hash: str) -> dict[str, object]:
        record: dict[str, object] = {
            "diagnostic_id": self.diagnostic_id,
            "setup_id": self.setup_id,
            "occurred_at_ms": self.occurred_at_ms,
            "symbol": self.symbol,
            "lane": self.lane,
            "side": None if self.side is None else self.side.value,
            "status": self.status.value,
            "reason": self.reason,
            "reference_price": self.reference_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "protocol_hash": protocol_hash,
            "features": dict(self.features),
        }
        return record


def _hash(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _completed_bars(bars: Sequence[Bar], now_ms: int) -> list[Bar]:
    completed = [bar for bar in bars if bar.closed_at_ms <= now_ms]
    return sorted(completed, key=lambda bar: (bar.closed_at_ms, bar.opened_at_ms))


def _active_levels(
    levels: Iterable[Level],
    symbol: str,
    now_ms: int,
    *,
    timeframes: set[str] | None = None,
) -> list[Level]:
    active = [
        level
        for level in levels
        if level.symbol == symbol
        and level.confirmed_at_ms <= now_ms
        and (level.broken_at_ms is None or level.broken_at_ms > now_ms)
        and (timeframes is None or level.timeframe in timeframes)
    ]
    return sorted(active, key=lambda level: (level.price, level.confirmed_at_ms, level.level_id))


def _nearest_level(
    levels: Iterable[Level],
    *,
    symbol: str,
    side: Side,
    price: float,
    now_ms: int,
    timeframes: set[str] | None = None,
) -> Level | None:
    active = _active_levels(levels, symbol, now_ms, timeframes=timeframes)
    if side is Side.LONG:
        eligible = [level for level in active if level.side is LevelSide.HIGH and level.price > price]
        return min(eligible, key=lambda level: level.price - price) if eligible else None
    eligible = [level for level in active if level.side is LevelSide.LOW and level.price < price]
    return min(eligible, key=lambda level: price - level.price) if eligible else None


def _bar_range_bp(bar: Bar) -> float | None:
    if bar.low <= 0 or bar.high <= bar.low:
        return None
    return (bar.high / bar.low - 1.0) * 10_000


def _body_to_range(bar: Bar) -> float | None:
    range_size = bar.high - bar.low
    if range_size <= 0:
        return None
    return abs(bar.close - bar.open) / range_size


def _median_abs_delta(bars: Sequence[Bar]) -> float | None:
    deltas = [abs(bar.delta_notional) for bar in bars if math.isfinite(bar.delta_notional)]
    if not deltas:
        return None
    value = float(median(deltas[-20:]))
    return value if value > 0 else None


def _touches(price: float, bars: Sequence[Bar]) -> int:
    return sum(1 for bar in bars if bar.low <= price <= bar.high)


class ShadowOrderflowEvaluator:
    """Causal, shadow-only evaluators for DOM density and terminal cascade hypotheses."""

    def __init__(
        self,
        *,
        tick_size: float = 0.01,
        min_wall_age_ms: int = 1_000,
        wall_failure_remaining_ratio: float = 0.5,
        min_cascade_body_to_range: float = 0.6,
        min_cascade_delta_ratio: float = 2.0,
        stop_buffer_bp: float = 5.0,
    ) -> None:
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if min_wall_age_ms < 0:
            raise ValueError("min_wall_age_ms must be non-negative")
        if not 0 < wall_failure_remaining_ratio < 1:
            raise ValueError("wall_failure_remaining_ratio must be between zero and one")
        if min_cascade_body_to_range <= 0 or min_cascade_delta_ratio <= 0:
            raise ValueError("cascade thresholds must be positive")
        if stop_buffer_bp < 0:
            raise ValueError("stop_buffer_bp must be non-negative")
        self.tick_size = tick_size
        self.min_wall_age_ms = min_wall_age_ms
        self.wall_failure_remaining_ratio = wall_failure_remaining_ratio
        self.min_cascade_body_to_range = min_cascade_body_to_range
        self.min_cascade_delta_ratio = min_cascade_delta_ratio
        self.stop_buffer_bp = stop_buffer_bp

    def density_bounce_v1(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        wall: DensityWallEvidence,
        levels: Iterable[Level] = (),
    ) -> ShadowDiagnostic:
        lane = "density_bounce_v1"
        series = [
            bar for bar in self._preferred_bars(bars, now_ms, ("15s", "1m")) if bar.closed_at_ms >= wall.observed_at_ms
        ]
        setup_id = self._setup_seed(
            symbol=symbol,
            lane=lane,
            side=wall.side,
            reference_price=wall.price,
            extra={
                "observed_at_ms": wall.observed_at_ms,
                "source_event_id": wall.source_event_id or "",
            },
        )
        if wall.symbol != symbol:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_wall_symbol_mismatch",
                features={"wall_symbol": wall.symbol},
            )
        if wall.observed_at_ms > now_ms:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_wall_observed_in_future",
                features={"wall_observed_at_ms": wall.observed_at_ms},
            )
        if wall.evidence_quality != "observed":
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_ambiguous_wall_removal",
                features={"evidence_quality": wall.evidence_quality},
            )
        if wall.wall_age_ms is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_missing_wall_age",
                features={},
            )
        if wall.initial_size is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_missing_initial_size",
                features={"wall_age_ms": float(wall.wall_age_ms)},
            )
        if wall.current_remaining is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_missing_current_remaining",
                features={"wall_age_ms": float(wall.wall_age_ms), "initial_size": float(wall.initial_size)},
            )
        if wall.wall_age_ms < 0 or wall.initial_size <= 0 or wall.current_remaining < 0:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_ambiguous_wall_evidence",
                features={
                    "wall_age_ms": float(wall.wall_age_ms),
                    "initial_size": float(wall.initial_size),
                    "current_remaining": float(wall.current_remaining),
                },
            )
        if wall.current_remaining > wall.initial_size:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_remaining_exceeds_initial_size",
                features={
                    "wall_age_ms": float(wall.wall_age_ms),
                    "initial_size": float(wall.initial_size),
                    "current_remaining": float(wall.current_remaining),
                },
            )
        if wall.wall_age_ms < self.min_wall_age_ms:
            features = {
                "wall_age_ms": float(wall.wall_age_ms),
                "initial_size": float(wall.initial_size),
                "current_remaining": float(wall.current_remaining),
                "remaining_ratio": float(wall.current_remaining / wall.initial_size),
            }
            return self._armed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=wall.side,
                reason="wall_too_young",
                reference_price=wall.price,
                stop_price=self._wall_stop(wall.price, wall.side),
                target_price=None,
                features=features,
            )

        latest = series[-1] if series else None
        previous = series[-2] if len(series) >= 2 else None
        candidate_side = wall.side
        active_target = _nearest_level(
            levels,
            symbol=symbol,
            side=candidate_side,
            price=wall.price,
            now_ms=now_ms,
            timeframes={"15m", "4h"},
        )
        target_price = active_target.price if active_target is not None else None
        stop_price = self._wall_stop(wall.price, candidate_side)
        touch_count = _touches(wall.price, series) if series else 0
        prior_touch_count = _touches(wall.price, series[:-1]) if len(series) > 1 else 0
        remaining_ratio = wall.current_remaining / wall.initial_size
        median_bar_volume = float(median([bar.volume for bar in series[-20:]])) if series else 0.0
        features: dict[str, float | str] = {
            "wall_age_ms": float(wall.wall_age_ms),
            "initial_size": float(wall.initial_size),
            "current_remaining": float(wall.current_remaining),
            "remaining_ratio": float(remaining_ratio),
            "touch_count": float(touch_count),
            "prior_touch_count": float(prior_touch_count),
            "bar_count": float(len(series)),
            "wall_price": float(wall.price),
            "wall_to_median_bar_volume": (
                float(wall.initial_size / median_bar_volume) if median_bar_volume > 0 else 0.0
            ),
            "target_source": "causal_level" if active_target is not None else "none",
        }
        if active_target is not None:
            features["target_level_id"] = active_target.level_id
            features["target_level_timeframe"] = active_target.timeframe

        if latest is None:
            return self._armed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="waiting_for_completed_bars",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features=features,
            )

        if remaining_ratio <= self.wall_failure_remaining_ratio:
            return self._invalidated(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="wall_eroded_to_half",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features=features,
            )

        if prior_touch_count > 0:
            return self._invalidated(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="first_approach_already_consumed",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features=features,
            )

        range_bp = _bar_range_bp(latest)
        if range_bp is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="blocked_invalid_bar_range",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features={**features, "bar_open": latest.open, "bar_high": latest.high, "bar_low": latest.low},
            )

        reclaim = (
            latest.close >= wall.price + self.tick_size
            if candidate_side is Side.LONG
            else latest.close <= wall.price - self.tick_size
        )
        invalidated = (
            latest.close <= wall.price - self.tick_size
            if candidate_side is Side.LONG
            else latest.close >= wall.price + self.tick_size
        )
        touch = latest.low <= wall.price <= latest.high
        flow_ok = latest.delta_notional > 0 if candidate_side is Side.LONG else latest.delta_notional < 0

        if invalidated:
            return self._invalidated(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="wall_broken",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features={**features, "delta_notional": float(latest.delta_notional), "range_bp": float(range_bp)},
            )
        if touch and reclaim and flow_ok:
            return self._observed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="favorable_reclaim",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features={
                    **features,
                    "delta_notional": float(latest.delta_notional),
                    "range_bp": float(range_bp),
                    "touch": 1.0,
                    "reclaim": 1.0,
                    "flow_ok": 1.0,
                    "previous_close": float(previous.close) if previous is not None else float(latest.close),
                },
            )
        if touch or reclaim:
            return self._armed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=candidate_side,
                reason="touch_without_confirmation" if touch else "awaiting_reclaim",
                reference_price=wall.price,
                stop_price=stop_price,
                target_price=target_price,
                features={
                    **features,
                    "delta_notional": float(latest.delta_notional),
                    "range_bp": float(range_bp),
                    "touch": 1.0 if touch else 0.0,
                    "reclaim": 1.0 if reclaim else 0.0,
                    "flow_ok": 1.0 if flow_ok else 0.0,
                    "bar_close": float(latest.close),
                },
            )
        return self._armed(
            symbol=symbol,
            lane=lane,
            setup_id=setup_id,
            now_ms=now_ms,
            side=candidate_side,
            reason="waiting_for_touch",
            reference_price=wall.price,
            stop_price=stop_price,
            target_price=target_price,
            features={
                **features,
                "delta_notional": float(latest.delta_notional),
                "range_bp": float(range_bp),
                "touch": 0.0,
                "reclaim": 0.0,
                "flow_ok": 1.0 if flow_ok else 0.0,
                "bar_close": float(latest.close),
            },
        )

    def cascade_terminal_exit(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Iterable[Level],
    ) -> ShadowDiagnostic:
        lane = "cascade_terminal_exit"
        series = self._preferred_bars(bars, now_ms, ("1m",))
        current = series[-1] if series else None
        previous = series[-2] if len(series) >= 2 else None
        setup_id = self._setup_seed(
            symbol=symbol,
            lane=lane,
            side=None,
            reference_price=current.close if current is not None else None,
            extra={
                "current_opened_at_ms": current.opened_at_ms if current is not None else "",
                "current_delta_notional": current.delta_notional if current is not None else "",
                "current_close": current.close if current is not None else "",
            },
        )
        if current is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_no_completed_1m_bars",
                features={},
            )
        if current.low <= 0 or current.high <= 0 or current.high < current.low:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_invalid_current_bar",
                features={
                    "bar_open": float(current.open),
                    "bar_high": float(current.high),
                    "bar_low": float(current.low),
                    "bar_close": float(current.close),
                },
            )
        if previous is None:
            return self._armed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=None,
                reason="waiting_for_second_bar",
                reference_price=current.close,
                stop_price=None,
                target_price=None,
                features={"bar_count": float(len(series))},
            )

        median_delta = _median_abs_delta(series[:-1])
        if median_delta is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_insufficient_delta_history",
                reference_price=current.close,
                features={
                    "bar_count": float(len(series)),
                    "current_delta_notional": float(current.delta_notional),
                },
            )
        body_to_range = _body_to_range(current)
        if body_to_range is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                reason="blocked_invalid_body_to_range",
                reference_price=current.close,
                features={
                    "bar_open": float(current.open),
                    "bar_high": float(current.high),
                    "bar_low": float(current.low),
                },
            )
        delta_ratio = abs(current.delta_notional) / median_delta
        long_context = (
            current.close > previous.high
            and current.delta_notional > 0
            and body_to_range >= self.min_cascade_body_to_range
            and delta_ratio >= self.min_cascade_delta_ratio
        )
        short_context = (
            current.close < previous.low
            and current.delta_notional < 0
            and body_to_range >= self.min_cascade_body_to_range
            and delta_ratio >= self.min_cascade_delta_ratio
        )
        side: Side | None
        if long_context:
            side = Side.LONG
        elif short_context:
            side = Side.SHORT
        else:
            return self._armed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=None,
                reason="waiting_for_terminal_context",
                reference_price=current.close,
                stop_price=None,
                target_price=None,
                features={
                    "body_to_range": float(body_to_range),
                    "delta_ratio": float(delta_ratio),
                    "current_delta_notional": float(current.delta_notional),
                    "median_abs_delta": float(median_delta),
                },
            )

        causal_before_bar = _active_levels(
            levels,
            symbol,
            previous.closed_at_ms,
            timeframes={"15m", "4h"},
        )
        crossed_levels = [
            level
            for level in causal_before_bar
            if (side is Side.LONG and level.side is LevelSide.HIGH and previous.close < level.price <= current.close)
            or (side is Side.SHORT and level.side is LevelSide.LOW and previous.close > level.price >= current.close)
        ]
        target_level = (
            max(crossed_levels, key=lambda item: item.price)
            if crossed_levels and side is Side.LONG
            else min(crossed_levels, key=lambda item: item.price)
            if crossed_levels
            else _nearest_level(
                levels,
                symbol=symbol,
                side=side,
                price=current.close,
                now_ms=now_ms,
                timeframes={"15m", "4h"},
            )
        )
        if target_level is None:
            return self._blocked(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=side,
                reason="blocked_no_causal_active_level",
                reference_price=current.close,
                features={
                    "body_to_range": float(body_to_range),
                    "delta_ratio": float(delta_ratio),
                    "current_delta_notional": float(current.delta_notional),
                    "median_abs_delta": float(median_delta),
                },
            )

        crossed = (
            previous.close < target_level.price <= current.close
            if side is Side.LONG
            else previous.close > target_level.price >= current.close
        )
        stop_price = self._cascade_stop(current, side)
        target_price = target_level.price
        features = {
            "body_to_range": float(body_to_range),
            "delta_ratio": float(delta_ratio),
            "current_delta_notional": float(current.delta_notional),
            "median_abs_delta": float(median_delta),
            "target_level_id": target_level.level_id,
            "target_level_timeframe": target_level.timeframe,
            "target_level_price": float(target_level.price),
            "crossed": 1.0 if crossed else 0.0,
        }
        if crossed:
            return self._observed(
                symbol=symbol,
                lane=lane,
                setup_id=setup_id,
                now_ms=now_ms,
                side=side,
                reason="frozen_rules_met",
                reference_price=current.close,
                stop_price=stop_price,
                target_price=target_price,
                features=features,
            )
        return self._armed(
            symbol=symbol,
            lane=lane,
            setup_id=setup_id,
            now_ms=now_ms,
            side=side,
            reason="waiting_for_close_through",
            reference_price=current.close,
            stop_price=stop_price,
            target_price=target_price,
            features=features,
        )

    def _preferred_bars(
        self,
        bars: Mapping[str, Sequence[Bar]],
        now_ms: int,
        preferred_timeframes: Sequence[str],
    ) -> list[Bar]:
        for timeframe in preferred_timeframes:
            series = bars.get(timeframe, ())
            completed = _completed_bars(series, now_ms)
            if completed:
                return completed
        return []

    def _wall_stop(self, price: float, side: Side) -> float:
        if side is Side.LONG:
            return price - self.tick_size
        return price + self.tick_size

    def _cascade_stop(self, bar: Bar, side: Side) -> float:
        if side is Side.LONG:
            return bar.low * (1 - self.stop_buffer_bp / 10_000)
        return bar.high * (1 + self.stop_buffer_bp / 10_000)

    def _setup_seed(
        self,
        *,
        symbol: str,
        lane: str,
        side: Side | None,
        reference_price: float | None,
        extra: Mapping[str, object],
    ) -> str:
        parts: list[object] = [symbol, lane, reference_price]
        parts.append(None if side is None else side.value)
        for key in sorted(extra):
            parts.append(key)
            parts.append(extra[key])
        return _hash(*parts)

    def _diagnostic_id(self, setup_id: str, occurred_at_ms: int, status: ShadowStatus, reason: str) -> str:
        del occurred_at_ms
        return _hash(setup_id, status.value, reason)

    def _observed(
        self,
        *,
        symbol: str,
        lane: str,
        setup_id: str,
        now_ms: int,
        side: Side | None,
        reason: str,
        reference_price: float | None,
        stop_price: float | None,
        target_price: float | None,
        features: Mapping[str, float | str],
    ) -> ShadowDiagnostic:
        return ShadowDiagnostic(
            diagnostic_id=self._diagnostic_id(setup_id, now_ms, ShadowStatus.OBSERVED, reason),
            setup_id=setup_id,
            occurred_at_ms=now_ms,
            symbol=symbol,
            lane=lane,
            side=side,
            status=ShadowStatus.OBSERVED,
            reason=reason,
            reference_price=reference_price,
            stop_price=stop_price,
            target_price=target_price,
            features=features,
        )

    def _armed(
        self,
        *,
        symbol: str,
        lane: str,
        setup_id: str,
        now_ms: int,
        side: Side | None,
        reason: str,
        reference_price: float | None,
        stop_price: float | None,
        target_price: float | None,
        features: Mapping[str, float | str],
    ) -> ShadowDiagnostic:
        return ShadowDiagnostic(
            diagnostic_id=self._diagnostic_id(setup_id, now_ms, ShadowStatus.ARMED, reason),
            setup_id=setup_id,
            occurred_at_ms=now_ms,
            symbol=symbol,
            lane=lane,
            side=side,
            status=ShadowStatus.ARMED,
            reason=reason,
            reference_price=reference_price,
            stop_price=stop_price,
            target_price=target_price,
            features=features,
        )

    def _blocked(
        self,
        *,
        symbol: str,
        lane: str,
        setup_id: str,
        now_ms: int,
        reason: str,
        features: Mapping[str, float | str],
        side: Side | None = None,
        reference_price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> ShadowDiagnostic:
        return ShadowDiagnostic(
            diagnostic_id=self._diagnostic_id(setup_id, now_ms, ShadowStatus.BLOCKED, reason),
            setup_id=setup_id,
            occurred_at_ms=now_ms,
            symbol=symbol,
            lane=lane,
            side=side,
            status=ShadowStatus.BLOCKED,
            reason=reason,
            reference_price=reference_price,
            stop_price=stop_price,
            target_price=target_price,
            features=features,
        )

    def _invalidated(
        self,
        *,
        symbol: str,
        lane: str,
        setup_id: str,
        now_ms: int,
        side: Side | None,
        reason: str,
        reference_price: float | None,
        stop_price: float | None,
        target_price: float | None,
        features: Mapping[str, float | str],
    ) -> ShadowDiagnostic:
        return ShadowDiagnostic(
            diagnostic_id=self._diagnostic_id(setup_id, now_ms, ShadowStatus.INVALIDATED, reason),
            setup_id=setup_id,
            occurred_at_ms=now_ms,
            symbol=symbol,
            lane=lane,
            side=side,
            status=ShadowStatus.INVALIDATED,
            reason=reason,
            reference_price=reference_price,
            stop_price=stop_price,
            target_price=target_price,
            features=features,
        )
