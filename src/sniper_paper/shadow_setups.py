"""Shadow-only diagnostics for causal retest/reclaim level setups.

The module intentionally stays read-only:

- it only consumes completed bars and causally confirmed levels;
- it never creates paper signals, orders, or storage writes;
- it treats a level as broken only when the break timestamp is causal;
- it can be used as a standalone diagnostic lens without integration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .market import Bar
from .paper import Side
from .strategy import Level, LevelSide, apply_level_breaks


class ShadowSetupStatus(str, Enum):
    """Public lifecycle states for the shadow-only setup."""

    REJECTED = "REJECTED"
    ARMED = "ARMED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class ShadowSetupPhase(str, Enum):
    """Most advanced causal phase reached by the setup."""

    WAITING_BREAK = "waiting_break"
    BROKEN = "broken"
    RETESTED = "retested"
    RECLAIMED = "reclaimed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class RetestReclaimShadowSetup:
    """A single causal retest/reclaim diagnostic row."""

    setup_id: str
    symbol: str
    lane: str
    side: Side
    level_id: str
    level_timeframe: str
    level_price: float
    level_confirmed_at_ms: int
    broken_at_ms: int | None
    retest_at_ms: int | None
    reclaim_at_ms: int | None
    invalidated_at_ms: int | None
    status: ShadowSetupStatus
    phase: ShadowSetupPhase
    reason: str
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    features: Mapping[str, float | int | str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float | int | str | None | dict[str, float | int | str]]:
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "lane": self.lane,
            "side": self.side.value,
            "level_id": self.level_id,
            "level_timeframe": self.level_timeframe,
            "level_price": self.level_price,
            "level_confirmed_at_ms": self.level_confirmed_at_ms,
            "broken_at_ms": self.broken_at_ms,
            "retest_at_ms": self.retest_at_ms,
            "reclaim_at_ms": self.reclaim_at_ms,
            "invalidated_at_ms": self.invalidated_at_ms,
            "status": self.status.value,
            "phase": self.phase.value,
            "reason": self.reason,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "features": dict(self.features),
        }


def _hash(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _completed_bars(bars: Sequence[Bar], symbol: str, now_ms: int) -> list[Bar]:
    return sorted(
        (bar for bar in bars if bar.symbol == symbol and bar.closed_at_ms <= now_ms),
        key=lambda bar: (bar.closed_at_ms, bar.opened_at_ms),
    )


def _merge_bars(*sequences: Sequence[Bar], symbol: str, now_ms: int) -> list[Bar]:
    merged: list[Bar] = []
    seen: set[tuple[str, int, int]] = set()
    for sequence in sequences:
        for bar in _completed_bars(sequence, symbol, now_ms):
            key = (bar.symbol, bar.timeframe_ms, bar.opened_at_ms)
            if key in seen:
                continue
            seen.add(key)
            merged.append(bar)
    return sorted(merged, key=lambda bar: (bar.closed_at_ms, bar.opened_at_ms, bar.timeframe_ms))


def _bar_contains_level(level: Level, bar: Bar, tolerance: float) -> bool:
    if level.side is LevelSide.HIGH:
        return bar.low <= level.price + tolerance
    return bar.high >= level.price - tolerance


def _trade_side(level: Level) -> Side:
    return Side.LONG if level.side is LevelSide.HIGH else Side.SHORT


def _nearest_causal_target(
    levels: Sequence[Level], *, symbol: str, side: Side, entry_price: float, now_ms: int
) -> Level | None:
    candidates = [
        level
        for level in levels
        if level.symbol == symbol
        and level.confirmed_at_ms <= now_ms
        and (level.broken_at_ms is None or level.broken_at_ms > now_ms)
        and (
            (side is Side.LONG and level.side is LevelSide.HIGH and level.price > entry_price)
            or (side is Side.SHORT and level.side is LevelSide.LOW and level.price < entry_price)
        )
    ]
    return min(candidates, key=lambda item: abs(item.price - entry_price)) if candidates else None


class RetestReclaimShadowEvaluator:
    """Shadow-only evaluator for causal retest/reclaim diagnostics.

    The evaluator inspects only completed bars and levels that are already
    causally confirmed as of ``now_ms``. It never emits a tradable signal.
    """

    def __init__(
        self,
        *,
        tick_size: float = 0.01,
        fast_timeframe: str = "1m",
        fast_confirming_closes: int = 2,
        source_confirming_closes: int = 1,
        break_buffer_ticks: int = 1,
        retest_tolerance_ticks: int = 1,
        reclaim_buffer_ticks: int = 1,
        max_setup_age_ms: int = 30 * 60_000,
    ) -> None:
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if fast_confirming_closes < 1 or source_confirming_closes < 1:
            raise ValueError("confirming close counts must be positive")
        if break_buffer_ticks < 1 or retest_tolerance_ticks < 0 or reclaim_buffer_ticks < 1:
            raise ValueError("tick parameters must be valid")
        if max_setup_age_ms <= 0:
            raise ValueError("max_setup_age_ms must be positive")
        self.tick_size = tick_size
        self.fast_timeframe = fast_timeframe
        self.fast_confirming_closes = fast_confirming_closes
        self.source_confirming_closes = source_confirming_closes
        self.break_buffer_ticks = break_buffer_ticks
        self.retest_tolerance_ticks = retest_tolerance_ticks
        self.reclaim_buffer_ticks = reclaim_buffer_ticks
        self.max_setup_age_ms = max_setup_age_ms

    def evaluate(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        levels: Iterable[Level],
    ) -> list[RetestReclaimShadowSetup]:
        completed = {timeframe: _completed_bars(sequence, symbol, now_ms) for timeframe, sequence in bars.items()}
        causal_levels = apply_level_breaks(
            [
                level
                for level in levels
                if level.symbol == symbol
                and level.confirmed_at_ms <= now_ms
                and (level.broken_at_ms is None or level.broken_at_ms <= now_ms)
            ],
            completed,
            now_ms,
            tick_size=self.tick_size,
            buffer_ticks=self.break_buffer_ticks,
            fast_timeframe=self.fast_timeframe,
            fast_confirming_closes=self.fast_confirming_closes,
            source_confirming_closes=self.source_confirming_closes,
        )

        result: list[RetestReclaimShadowSetup] = []
        for level in causal_levels:
            result.append(
                self._diagnose_level(
                    symbol=symbol,
                    now_ms=now_ms,
                    bars=completed,
                    level=level,
                    all_levels=causal_levels,
                )
            )
        return result

    def _diagnose_level(
        self,
        *,
        symbol: str,
        now_ms: int,
        bars: Mapping[str, Sequence[Bar]],
        level: Level,
        all_levels: Sequence[Level],
    ) -> RetestReclaimShadowSetup:
        lane = "retest_reclaim_v1"
        level_bars = _completed_bars(bars.get(self.fast_timeframe, ()), symbol, now_ms)
        retest_tolerance = self.tick_size * self.retest_tolerance_ticks
        reclaim_buffer = self.tick_size * self.reclaim_buffer_ticks
        side = _trade_side(level)

        if level.broken_at_ms is not None and level.broken_at_ms > now_ms:
            return RetestReclaimShadowSetup(
                setup_id=_hash(symbol, lane, level.level_id, "future_break"),
                symbol=symbol,
                lane=lane,
                side=side,
                level_id=level.level_id,
                level_timeframe=level.timeframe,
                level_price=level.price,
                level_confirmed_at_ms=level.confirmed_at_ms,
                broken_at_ms=None,
                retest_at_ms=None,
                reclaim_at_ms=None,
                invalidated_at_ms=None,
                status=ShadowSetupStatus.REJECTED,
                phase=ShadowSetupPhase.WAITING_BREAK,
                reason="future_broken_timestamp",
                entry_price=None,
                stop_price=None,
                target_price=None,
                features={"level_broken_at_ms": level.broken_at_ms},
            )

        broken_at_ms = level.broken_at_ms
        if broken_at_ms is None:
            broken_at_ms = self._first_break_at(level, bars, now_ms)
        if broken_at_ms is None:
            return RetestReclaimShadowSetup(
                setup_id=_hash(symbol, lane, level.level_id, "waiting_break"),
                symbol=symbol,
                lane=lane,
                side=side,
                level_id=level.level_id,
                level_timeframe=level.timeframe,
                level_price=level.price,
                level_confirmed_at_ms=level.confirmed_at_ms,
                broken_at_ms=None,
                retest_at_ms=None,
                reclaim_at_ms=None,
                invalidated_at_ms=None,
                status=ShadowSetupStatus.REJECTED,
                phase=ShadowSetupPhase.WAITING_BREAK,
                reason="waiting_break",
                entry_price=None,
                stop_price=None,
                target_price=None,
                features={"break_buffer_ticks": self.break_buffer_ticks},
            )

        deadline_ms = broken_at_ms + self.max_setup_age_ms
        causal_window_end_ms = min(now_ms, deadline_ms)
        retest_bar = self._first_retest(level, level_bars, broken_at_ms, causal_window_end_ms, retest_tolerance)
        retest_at_ms = retest_bar.closed_at_ms if retest_bar else None
        reclaim_bar, invalidated_bar = self._outcome_after_retest(
            level, level_bars, retest_bar, causal_window_end_ms, reclaim_buffer
        )
        reclaim_at_ms = reclaim_bar.closed_at_ms if reclaim_bar else None
        invalidated_at_ms = invalidated_bar.closed_at_ms if invalidated_bar else None
        if reclaim_at_ms is not None:
            assert reclaim_bar is not None and retest_bar is not None
            entry_price = reclaim_bar.close
            stop_price = retest_bar.low - self.tick_size if side is Side.LONG else retest_bar.high + self.tick_size
            target = _nearest_causal_target(
                all_levels, symbol=symbol, side=side, entry_price=entry_price, now_ms=reclaim_at_ms
            )
            target_price = target.price if target else None
            return RetestReclaimShadowSetup(
                setup_id=_hash(symbol, lane, level.level_id, broken_at_ms, retest_at_ms, reclaim_at_ms),
                symbol=symbol,
                lane=lane,
                side=side,
                level_id=level.level_id,
                level_timeframe=level.timeframe,
                level_price=level.price,
                level_confirmed_at_ms=level.confirmed_at_ms,
                broken_at_ms=broken_at_ms,
                retest_at_ms=retest_at_ms,
                reclaim_at_ms=reclaim_at_ms,
                invalidated_at_ms=None,
                status=ShadowSetupStatus.CONFIRMED,
                phase=ShadowSetupPhase.RECLAIMED,
                reason="reclaimed" if target else "reclaimed_no_causal_target",
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                features={
                    "retest_tolerance_ticks": self.retest_tolerance_ticks,
                    "reclaim_buffer_ticks": self.reclaim_buffer_ticks,
                    "entry_to_stop_bp": abs(entry_price / stop_price - 1.0) * 10_000 if stop_price else 0.0,
                    "target_level_id": target.level_id if target else "none",
                },
            )

        if invalidated_at_ms is not None:
            assert invalidated_bar is not None and retest_bar is not None
            stop_price = retest_bar.low - self.tick_size if side is Side.LONG else retest_bar.high + self.tick_size
            return RetestReclaimShadowSetup(
                setup_id=_hash(symbol, lane, level.level_id, broken_at_ms, invalidated_at_ms),
                symbol=symbol,
                lane=lane,
                side=side,
                level_id=level.level_id,
                level_timeframe=level.timeframe,
                level_price=level.price,
                level_confirmed_at_ms=level.confirmed_at_ms,
                broken_at_ms=broken_at_ms,
                retest_at_ms=retest_at_ms,
                reclaim_at_ms=None,
                invalidated_at_ms=invalidated_at_ms,
                status=ShadowSetupStatus.INVALIDATED,
                phase=ShadowSetupPhase.INVALIDATED,
                reason="invalidated_before_reclaim",
                entry_price=invalidated_bar.close,
                stop_price=stop_price,
                target_price=None,
                features={
                    "retest_tolerance_ticks": self.retest_tolerance_ticks,
                    "reclaim_buffer_ticks": self.reclaim_buffer_ticks,
                },
            )

        if now_ms - broken_at_ms > self.max_setup_age_ms:
            return RetestReclaimShadowSetup(
                setup_id=_hash(symbol, lane, level.level_id, broken_at_ms, "expired"),
                symbol=symbol,
                lane=lane,
                side=side,
                level_id=level.level_id,
                level_timeframe=level.timeframe,
                level_price=level.price,
                level_confirmed_at_ms=level.confirmed_at_ms,
                broken_at_ms=broken_at_ms,
                retest_at_ms=retest_at_ms,
                reclaim_at_ms=None,
                invalidated_at_ms=None,
                status=ShadowSetupStatus.EXPIRED,
                phase=ShadowSetupPhase.EXPIRED,
                reason="setup_expired",
                entry_price=None,
                stop_price=None,
                target_price=None,
                features={"max_setup_age_ms": self.max_setup_age_ms},
            )

        return RetestReclaimShadowSetup(
            setup_id=_hash(symbol, lane, level.level_id, broken_at_ms, "armed"),
            symbol=symbol,
            lane=lane,
            side=side,
            level_id=level.level_id,
            level_timeframe=level.timeframe,
            level_price=level.price,
            level_confirmed_at_ms=level.confirmed_at_ms,
            broken_at_ms=broken_at_ms,
            retest_at_ms=retest_at_ms,
            reclaim_at_ms=None,
            invalidated_at_ms=None,
            status=ShadowSetupStatus.ARMED,
            phase=ShadowSetupPhase.RETESTED if retest_at_ms is not None else ShadowSetupPhase.BROKEN,
            reason="waiting_reclaim" if retest_at_ms is not None else "waiting_retest",
            entry_price=None,
            stop_price=(
                retest_bar.low - self.tick_size
                if retest_bar is not None and side is Side.LONG
                else retest_bar.high + self.tick_size
                if retest_bar is not None
                else None
            ),
            target_price=None,
            features={
                "retest_tolerance_ticks": self.retest_tolerance_ticks,
                "reclaim_buffer_ticks": self.reclaim_buffer_ticks,
            },
        )

    def _first_break_at(self, level: Level, bars: Mapping[str, Sequence[Bar]], now_ms: int) -> int | None:
        if level.timeframe == self.fast_timeframe:
            sequences = bars.get(self.fast_timeframe, ())
        else:
            sequences = bars.get(level.timeframe, ())
        broken = apply_level_breaks(
            [level],
            {level.timeframe: sequences, self.fast_timeframe: bars.get(self.fast_timeframe, ())},
            now_ms,
            tick_size=self.tick_size,
            buffer_ticks=self.break_buffer_ticks,
            fast_timeframe=self.fast_timeframe,
            fast_confirming_closes=self.fast_confirming_closes,
            source_confirming_closes=self.source_confirming_closes,
        )[0]
        return broken.broken_at_ms

    def _first_retest(
        self,
        level: Level,
        bars: Sequence[Bar],
        broken_at_ms: int,
        now_ms: int,
        retest_tolerance: float,
    ) -> Bar | None:
        for bar in bars:
            if bar.opened_at_ms < broken_at_ms or bar.closed_at_ms > now_ms:
                continue
            if _bar_contains_level(level, bar, retest_tolerance):
                if level.side is LevelSide.HIGH and bar.close >= level.price - retest_tolerance:
                    return bar
                if level.side is LevelSide.LOW and bar.close <= level.price + retest_tolerance:
                    return bar
        return None

    def _outcome_after_retest(
        self,
        level: Level,
        bars: Sequence[Bar],
        retest: Bar | None,
        now_ms: int,
        reclaim_buffer: float,
    ) -> tuple[Bar | None, Bar | None]:
        if retest is None:
            return None, None
        for bar in bars:
            if bar.opened_at_ms < retest.closed_at_ms or bar.closed_at_ms > now_ms:
                continue
            if level.side is LevelSide.HIGH:
                if bar.close <= retest.low - reclaim_buffer:
                    return None, bar
                if bar.close >= retest.high + reclaim_buffer:
                    return bar, None
            else:
                if bar.close >= retest.high + reclaim_buffer:
                    return None, bar
                if bar.close <= retest.low - reclaim_buffer:
                    return bar, None
        return None, None


def diagnose_retest_reclaim_v1(
    *,
    symbol: str,
    now_ms: int,
    bars: Mapping[str, Sequence[Bar]],
    levels: Iterable[Level],
    tick_size: float = 0.01,
    fast_timeframe: str = "1m",
    fast_confirming_closes: int = 2,
    source_confirming_closes: int = 1,
    break_buffer_ticks: int = 1,
    retest_tolerance_ticks: int = 1,
    reclaim_buffer_ticks: int = 1,
    max_setup_age_ms: int = 30 * 60_000,
) -> list[RetestReclaimShadowSetup]:
    """Convenience wrapper around :class:`RetestReclaimShadowEvaluator`."""

    evaluator = RetestReclaimShadowEvaluator(
        tick_size=tick_size,
        fast_timeframe=fast_timeframe,
        fast_confirming_closes=fast_confirming_closes,
        source_confirming_closes=source_confirming_closes,
        break_buffer_ticks=break_buffer_ticks,
        retest_tolerance_ticks=retest_tolerance_ticks,
        reclaim_buffer_ticks=reclaim_buffer_ticks,
        max_setup_age_ms=max_setup_age_ms,
    )
    return evaluator.evaluate(symbol=symbol, now_ms=now_ms, bars=bars, levels=levels)


__all__ = [
    "RetestReclaimShadowEvaluator",
    "RetestReclaimShadowSetup",
    "ShadowSetupPhase",
    "ShadowSetupStatus",
    "diagnose_retest_reclaim_v1",
]
