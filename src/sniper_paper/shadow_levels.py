"""Shadow/reference Digash horizontal-level geometry.

This is an explicit, causal adaptation for research comparison.  It is not a
claim to reproduce Digash's undisclosed pivot contract and is intentionally not
connected to the trading evaluator.  Only completed :class:`~sniper_paper.market.Bar`
objects supplied by the caller are considered.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from .levels import Level, LevelSide
from .market import Bar

BreakRule = Literal["wick", "close"]

TIMEFRAME_MS: Mapping[str, int] = {
    "15s": 15_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "4h": 14_400_000,
}


@dataclass(frozen=True, slots=True)
class GeometryConfig:
    """Parameters for the documented shadow adaptation.

    ``merge_tolerance_bp`` may be a scalar or a timeframe keyed mapping.  A
    keyed mapping makes it possible to compare per-timeframe tolerances without
    silently applying one instrument scale to every timeframe.
    """

    history_limit: int = 1_000
    extremum_search_period: int = 40
    right_exclusion: int = 20
    merge_tolerance_bp: float | Mapping[str, float] = 10.0
    break_rule: BreakRule = "close"
    break_buffer_bp: float = 0.0
    level_version: str = "digash_shadow_adaptation_v1"

    def tolerance_for(self, timeframe: str) -> float:
        raw = (
            self.merge_tolerance_bp.get(timeframe, 10.0)
            if isinstance(self.merge_tolerance_bp, Mapping)
            else self.merge_tolerance_bp
        )
        tolerance = float(raw)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("merge tolerance must be finite and non-negative")
        return tolerance

    def validate(self, timeframe: str) -> None:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        if self.history_limit < 1 or self.extremum_search_period < 3 or self.right_exclusion < 0:
            raise ValueError("history_limit must be positive; search period must be >= 3; exclusion non-negative")
        if self.break_rule not in ("wick", "close"):
            raise ValueError("break_rule must be 'wick' or 'close'")
        if not math.isfinite(self.break_buffer_bp) or self.break_buffer_bp < 0:
            raise ValueError("break buffer must be finite and non-negative")
        if not self.level_version:
            raise ValueError("level_version is required")
        self.tolerance_for(timeframe)


@dataclass(frozen=True, slots=True)
class GeometryCoverage:
    timeframe: str
    interval_ms: int
    history_limit: int
    received_bars: int
    used_bars: int
    first_opened_at_ms: int | None
    last_opened_at_ms: int | None
    interior_gap_count: int
    interior_missing_bars: int
    gaps: tuple[dict[str, int], ...] = ()
    short_history: bool = False
    history_complete: bool = False


@dataclass(frozen=True, slots=True)
class GeometryResult:
    levels: tuple[Level, ...]
    coverage: GeometryCoverage


def build_reference_levels(
    bars: Iterable[Bar],
    *,
    timeframe: str,
    now_ms: int | None = None,
    config: GeometryConfig | None = None,
) -> GeometryResult:
    """Build causal reference levels and coverage metadata.

    The adaptation searches unique extrema in a centred window of
    ``extremum_search_period`` completed bars.  The final ``right_exclusion``
    bars are excluded from the search and a pivot is confirmed only once that
    many later bars have closed.  A candidate broken after its origin under the
    configured wick/close rule is omitted entirely from the result.
    """

    config = config or GeometryConfig()
    config.validate(timeframe)
    rows = _normalise_bars(bars, timeframe, now_ms)
    coverage = _coverage(rows, timeframe, config.history_limit)
    if len(rows) > config.history_limit:
        rows = rows[-config.history_limit :]
    if len(rows) < config.extremum_search_period + config.right_exclusion:
        return GeometryResult((), coverage)

    half_left = config.extremum_search_period // 2
    half_right = config.extremum_search_period - half_left - 1
    search_end = len(rows) - config.right_exclusion
    candidates: list[Level] = []
    for index in range(half_left, search_end):
        pivot = rows[index]
        window = rows[index - half_left : index + half_right + 1]
        high_values = [item.high for item in window]
        low_values = [item.low for item in window]
        if pivot.high == max(high_values) and high_values.count(pivot.high) == 1:
            confirmed_index = index + config.right_exclusion
            if not _broken_after_origin(rows, index, pivot.high, LevelSide.HIGH, config):
                candidates.append(
                    _candidate_level(pivot, LevelSide.HIGH, confirmed_index, rows[confirmed_index].closed_at_ms, timeframe, config)
                )
        if pivot.low == min(low_values) and low_values.count(pivot.low) == 1:
            confirmed_index = index + config.right_exclusion
            if not _broken_after_origin(rows, index, pivot.low, LevelSide.LOW, config):
                candidates.append(
                    _candidate_level(pivot, LevelSide.LOW, confirmed_index, rows[confirmed_index].closed_at_ms, timeframe, config)
                )

    merged = _merge_candidates(candidates, timeframe, config.tolerance_for(timeframe), config.level_version)
    return GeometryResult(tuple(merged), coverage)


def reference_levels(
    bars: Iterable[Bar],
    *,
    timeframe: str,
    now_ms: int | None = None,
    config: GeometryConfig | None = None,
) -> list[Level]:
    """Convenience list-returning wrapper around :func:`build_reference_levels`."""

    return list(build_reference_levels(bars, timeframe=timeframe, now_ms=now_ms, config=config).levels)


def _normalise_bars(bars: Iterable[Bar], timeframe: str, now_ms: int | None) -> list[Bar]:
    interval = TIMEFRAME_MS[timeframe]
    selected: dict[int, Bar] = {}
    for bar in bars:
        if bar.timeframe_ms != interval or (now_ms is not None and bar.closed_at_ms > now_ms):
            continue
        if bar.closed_at_ms <= bar.opened_at_ms or bar.opened_at_ms < 0:
            continue
        selected[bar.opened_at_ms] = bar
    return sorted(selected.values(), key=lambda item: item.opened_at_ms)


def _coverage(rows: Sequence[Bar], timeframe: str, history_limit: int) -> GeometryCoverage:
    interval = TIMEFRAME_MS[timeframe]
    gaps: list[dict[str, int]] = []
    for previous, current in pairwise(rows):
        missing = (current.opened_at_ms - previous.opened_at_ms) // interval - 1
        if missing > 0:
            gaps.append(
                {
                    "from_opened_at_ms": previous.opened_at_ms,
                    "to_opened_at_ms": current.opened_at_ms,
                    "missing_bars": missing,
                }
            )
    interior_missing = sum(item["missing_bars"] for item in gaps)
    short_history = len(rows) < history_limit
    return GeometryCoverage(
        timeframe=timeframe,
        interval_ms=interval,
        history_limit=history_limit,
        received_bars=len(rows),
        used_bars=min(len(rows), history_limit),
        first_opened_at_ms=rows[0].opened_at_ms if rows else None,
        last_opened_at_ms=rows[-1].opened_at_ms if rows else None,
        interior_gap_count=len(gaps),
        interior_missing_bars=interior_missing,
        gaps=tuple(gaps),
        short_history=short_history,
        history_complete=not short_history and not gaps,
    )


def _candidate_level(
    pivot: Bar,
    side: LevelSide,
    confirmed_index: int,
    confirmed_at_ms: int,
    timeframe: str,
    config: GeometryConfig,
) -> Level:
    raw = f"{pivot.symbol}:{timeframe}:shadow:{side.value}:{pivot.opened_at_ms}:{pivot.high if side is LevelSide.HIGH else pivot.low:.12g}"
    level_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
    price = pivot.high if side is LevelSide.HIGH else pivot.low
    return Level(
        level_id,
        pivot.symbol,
        timeframe,
        side,
        price,
        confirmed_at_ms,
        touches=1,
        level_class="shadow_extreme",
        origin_at_ms=pivot.opened_at_ms,
        zone_low=price,
        zone_high=price,
        level_version=config.level_version,
        # The extremum exists at origin time but is not observable as a level
        # until the full right-exclusion window has completed.
        first_seen_at_ms=confirmed_at_ms,
        provenance={
            "detector": "digash_shadow_adaptation",
            "extremum_search_period": config.extremum_search_period,
            "right_exclusion": config.right_exclusion,
            "confirmation_index": confirmed_index,
            "break_rule": config.break_rule,
        },
    )


def _broken_after_origin(
    rows: Sequence[Bar], origin_index: int, price: float, side: LevelSide, config: GeometryConfig
) -> bool:
    threshold = price * (1 + config.break_buffer_bp / 10_000) if side is LevelSide.HIGH else price * (1 - config.break_buffer_bp / 10_000)
    epsilon = max(abs(threshold) * 1e-12, 1e-12)
    for row in rows[origin_index + 1 :]:
        beyond = (
            row.high > threshold + epsilon if side is LevelSide.HIGH else row.low < threshold - epsilon
        ) if config.break_rule == "wick" else (
            row.close > threshold + epsilon if side is LevelSide.HIGH else row.close < threshold - epsilon
        )
        if beyond:
            return True
    return False


def _merge_candidates(
    candidates: Sequence[Level], timeframe: str, tolerance_bp: float, level_version: str
) -> list[Level]:
    result: list[Level] = []
    used: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: (item.side.value, item.price, item.origin_at_ms or 0, item.level_id)):
        if candidate.level_id in used:
            continue
        group = [
            item
            for item in candidates
            if item.level_id not in used
            and item.side is candidate.side
            and abs(item.price / candidate.price - 1.0) * 10_000 <= tolerance_bp
        ]
        used.update(item.level_id for item in group)
        if not group:
            continue
        prices = [item.price for item in group]
        zone_low, zone_high = min(prices), max(prices)
        ids = sorted(item.level_id for item in group)
        level_id = hashlib.sha256(f"shadow-cluster:{timeframe}:{':'.join(ids)}".encode()).hexdigest()[:20]
        origin = min(item.origin_at_ms or item.confirmed_at_ms for item in group)
        confirmed = max(item.confirmed_at_ms for item in group)
        result.append(
            Level(
                level_id,
                candidate.symbol,
                timeframe,
                candidate.side,
                min(prices) if candidate.side is LevelSide.HIGH else max(prices),
                confirmed,
                touches=sum(item.touches for item in group),
                level_class="shadow_cluster" if len(group) > 1 else "shadow_extreme",
                origin_at_ms=origin,
                zone_low=zone_low,
                zone_high=zone_high,
                level_version=level_version,
                first_seen_at_ms=confirmed,
                provenance={
                    "detector": "digash_shadow_adaptation",
                    "member_level_ids": ids,
                    "touch_events": sorted(item.origin_at_ms for item in group if item.origin_at_ms is not None),
                    "merge_tolerance_bp": tolerance_bp,
                },
            )
        )
    return sorted(result, key=lambda item: (item.side.value, item.price, item.origin_at_ms or 0, item.level_id))


__all__ = [
    "TIMEFRAME_MS",
    "BreakRule",
    "GeometryConfig",
    "GeometryCoverage",
    "GeometryResult",
    "build_reference_levels",
    "reference_levels",
]
