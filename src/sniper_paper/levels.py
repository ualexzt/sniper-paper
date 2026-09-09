"""Canonical causal horizontal-level model and target catalogue.

Both the chart and evaluators receive levels through this module.  Raw swing
levels remain useful as provenance for setup detection, but only canonical
families (higher-timeframe swings, clustered 15m swings, and previous-day
levels) are exposed by :func:`canonical_level_catalog` for target selection.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LevelSide(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass(frozen=True)
class Level:
    """An immutable, causally confirmed horizontal level.

    ``broken_at_ms`` is absorbing: it records the first confirmed close-through
    and must never be cleared by a later reclustering or restart.  ``origin``
    and ``confirmed`` deliberately remain separate so consumers cannot draw or
    trade a level before its pivot was confirmed.
    """

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
    zone_low: float | None = None
    zone_high: float | None = None
    revision: int = 1
    level_version: str = "legacy_level_v1"
    first_seen_at_ms: int | None = None
    invalidation_reason: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.level_id or not self.symbol or not self.timeframe:
            raise ValueError("level_id, symbol, and timeframe are required")
        if not isinstance(self.side, LevelSide):
            raise TypeError("side must be LevelSide")
        if not math.isfinite(self.price):
            raise ValueError("price must be finite")
        if self.confirmed_at_ms < 0:
            raise ValueError("confirmed_at_ms must be non-negative")
        if self.origin_at_ms is not None and self.origin_at_ms < 0:
            raise ValueError("origin_at_ms must be non-negative")
        if self.touches < 1:
            raise ValueError("touches must be positive")
        zone_low = self.price if self.zone_low is None else self.zone_low
        zone_high = self.price if self.zone_high is None else self.zone_high
        if not math.isfinite(zone_low) or not math.isfinite(zone_high) or zone_low > zone_high:
            raise ValueError("level zone must be finite and ordered")
        if not zone_low <= self.price <= zone_high:
            raise ValueError("level price must be inside its zone")
        if self.revision < 1 or not self.level_version:
            raise ValueError("level revision/version are required")
        if self.first_seen_at_ms is not None and self.first_seen_at_ms < 0:
            raise ValueError("first_seen_at_ms must be non-negative")
        if self.broken_at_ms is not None:
            if self.broken_at_ms < 0:
                raise ValueError("broken_at_ms must be non-negative")
            if self.broken_at_ms < self.confirmed_at_ms:
                raise ValueError("broken_at_ms cannot precede confirmed_at_ms")

    def active_at_ms(self, as_of_ms: int) -> bool:
        """Return whether this level was eligible at the supplied event time."""

        return level_active_at(self, as_of_ms)

    def status_at_ms(self, as_of_ms: int) -> str:
        """Return the causal status ``future``, ``active`` or ``broken``."""

        if as_of_ms < self.confirmed_at_ms:
            return "future"
        if self.broken_at_ms is not None and self.broken_at_ms <= as_of_ms:
            return "broken"
        return "active"


def level_active_at(level: Level, as_of_ms: int) -> bool:
    """Causal eligibility at an event timestamp.

    Scenario code can pass the timestamp immediately before a trigger to
    reference a level that was active before that trigger broke it.  This does
    not reactivate the level at later timestamps.
    """

    return level.confirmed_at_ms <= as_of_ms and (
        level.broken_at_ms is None or level.broken_at_ms > as_of_ms
    )


def cluster_levels(
    levels: Sequence[Level],
    direction: Any,
    *,
    tolerance_bp: float = 10.0,
    min_touches: int = 2,
) -> list[Level]:
    """Merge nearby same-side levels into deterministic target zones.

    ``direction`` is ``Side.LONG``/``Side.SHORT`` (or an equivalent value).
    The lower edge is used for long targets and the upper edge for short
    targets, retaining the previous strategy's conservative target boundary.
    """

    if tolerance_bp < 0 or min_touches < 1:
        raise ValueError("cluster parameters must be non-negative/positive")
    ordered = sorted(levels, key=lambda level: (level.price, level.confirmed_at_ms, level.level_id))
    result: list[Level] = []
    used: set[str] = set()
    is_long = getattr(direction, "value", direction) == "LONG"
    for level in ordered:
        if level.level_id in used:
            continue
        group = [
            candidate
            for candidate in ordered
            if candidate.level_id not in used
            and abs(candidate.price / level.price - 1.0) * 10_000 <= tolerance_bp
        ]
        if sum(candidate.touches for candidate in group) < min_touches:
            continue
        used.update(candidate.level_id for candidate in group)
        zone_low = min(candidate.zone_low if candidate.zone_low is not None else candidate.price for candidate in group)
        zone_high = max(candidate.zone_high if candidate.zone_high is not None else candidate.price for candidate in group)
        target_price = zone_low if is_long else zone_high
        boundary_members = [
            candidate
            for candidate in group
            if (
                candidate.zone_low if candidate.zone_low is not None else candidate.price
            )
            == target_price
            or (
                candidate.zone_high if candidate.zone_high is not None else candidate.price
            )
            == target_price
        ]
        origin = min(
            candidate.origin_at_ms if candidate.origin_at_ms is not None else candidate.confirmed_at_ms
            for candidate in boundary_members
        )
        ids = ":".join(sorted(candidate.level_id for candidate in group))
        versions = {candidate.level_version for candidate in group}
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
                origin,
                zone_low=zone_low,
                zone_high=zone_high,
                revision=max(candidate.revision for candidate in group),
                level_version=versions.pop() if len(versions) == 1 else "mixed_level_versions",
                # A multi-member zone is first observable only after its last
                # required constituent has itself been confirmed.
                first_seen_at_ms=max(candidate.confirmed_at_ms for candidate in group),
                provenance={"member_level_ids": sorted(candidate.level_id for candidate in group)},
            )
        )
    return result


def canonical_level_catalog(
    levels: Iterable[Level],
    symbol: str,
    now_ms: int,
    *,
    cluster_tolerance_bp: float = 10.0,
    min_cluster_touches: int = 2,
) -> list[Level]:
    """Return the single deterministic set of currently eligible target levels.

    The catalog intentionally excludes inactive/future levels and unclustered
    15m swings.  It contains all 4h levels, previous-day high/low levels, and
    qualifying 15m clusters.  Ordering is stable across callers and process
    restarts; the view function below is responsible only for proximity/limit
    presentation.
    """

    active = [level for level in levels if level.symbol == symbol and level_active_at(level, now_ms)]
    four_hour = [level for level in active if level.timeframe == "4h"]
    previous_day = [
        level for level in active if level.timeframe == "15m" and level.level_class == "previous_day"
    ]
    existing_clusters = [
        level for level in active if level.timeframe == "15m" and level.level_class == "cluster"
    ]
    highs = [
        level
        for level in active
        if level.timeframe == "15m" and level.level_class == "swing" and level.side is LevelSide.HIGH
    ]
    lows = [
        level
        for level in active
        if level.timeframe == "15m" and level.level_class == "swing" and level.side is LevelSide.LOW
    ]
    clustered = cluster_levels(
        highs,
        "LONG",
        tolerance_bp=cluster_tolerance_bp,
        min_touches=min_cluster_touches,
    ) + cluster_levels(
        lows,
        "SHORT",
        tolerance_bp=cluster_tolerance_bp,
        min_touches=min_cluster_touches,
    )
    unique = {level.level_id: level for level in [*four_hour, *previous_day, *existing_clusters, *clustered]}
    return sorted(
        unique.values(),
        key=lambda level: (
            level.timeframe,
            level.side.value,
            level.price,
            level.confirmed_at_ms,
            level.level_id,
        ),
    )


def canonical_level_view(
    levels: Iterable[Level],
    symbol: str,
    price: float,
    now_ms: int,
    limit: int = 8,
    **kwargs: Any,
) -> list[Level]:
    """Return the nearest presentation slice of :func:`canonical_level_catalog`."""

    if limit < 0:
        raise ValueError("limit must be non-negative")
    catalog = canonical_level_catalog(levels, symbol, now_ms, **kwargs)
    return sorted(catalog, key=lambda level: (abs(level.price - price), -level.confirmed_at_ms, level.level_id))[:limit]


__all__ = [
    "Level",
    "LevelSide",
    "canonical_level_catalog",
    "canonical_level_view",
    "cluster_levels",
    "level_active_at",
]
