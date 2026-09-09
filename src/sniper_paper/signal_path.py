"""Causal signal-path diagnostics, explicitly separate from execution results."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

Side = Literal["buy", "sell"]


class PathStatus(str, Enum):
    TP_TOUCHED = "TP_TOUCHED"
    SL_TOUCHED = "SL_TOUCHED"
    TIMEOUT = "TIMEOUT"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"
    NO_TOUCH = "NO_TOUCH"


@dataclass(frozen=True, slots=True)
class TradeTick:
    occurred_at_ms: int
    price: float


@dataclass(frozen=True, slots=True)
class CompletedBar:
    opened_at_ms: int
    closed_at_ms: int
    high: float
    low: float
    complete: bool = True


@dataclass(frozen=True, slots=True)
class SignalPathResult:
    signal_id: str
    side: Side
    entry_price: float
    target_price: float
    stop_price: float
    start_at_ms: int
    status: PathStatus
    first_touch_at_ms: int | None
    first_touch_price: float | None
    mfe_price: float
    mae_price: float
    mfe_bps: float
    mae_bps: float
    covered_until_ms: int | None
    coverage_complete: bool
    coverage_reason: str

    def to_dict(self) -> dict[str, object]:
        """Return journal-friendly fields; deliberately no fill/P&L labels."""
        return {
            "signal_id": self.signal_id,
            "side": self.side,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "start_at_ms": self.start_at_ms,
            "status": self.status.value,
            "first_touch_at_ms": self.first_touch_at_ms,
            "first_touch_price": self.first_touch_price,
            "mfe_price": self.mfe_price,
            "mae_price": self.mae_price,
            "mfe_bps": self.mfe_bps,
            "mae_bps": self.mae_bps,
            "covered_until_ms": self.covered_until_ms,
            "coverage_complete": self.coverage_complete,
            "coverage_reason": self.coverage_reason,
        }


class SignalPathTracker:
    """Evaluate the first causal target/stop path after one fixed signal."""

    def __init__(
        self,
        signal_id: str,
        *,
        side: Side,
        entry_price: float,
        target_price: float,
        stop_price: float,
        start_at_ms: int,
    ) -> None:
        if not signal_id:
            raise ValueError("signal_id is required")
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        values = (entry_price, target_price, stop_price)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("prices must be finite and positive")
        if start_at_ms < 0:
            raise ValueError("start_at_ms must be non-negative")
        if side == "buy" and not stop_price < entry_price < target_price:
            raise ValueError("buy prices must satisfy stop < entry < target")
        if side == "sell" and not target_price < entry_price < stop_price:
            raise ValueError("sell prices must satisfy target < entry < stop")
        self.signal_id = signal_id
        self.side = side
        self.entry_price = float(entry_price)
        self.target_price = float(target_price)
        self.stop_price = float(stop_price)
        self.start_at_ms = start_at_ms

    def evaluate(
        self,
        *,
        trades: tuple[TradeTick, ...] = (),
        bars: tuple[CompletedBar, ...] = (),
        timeout_at_ms: int | None = None,
        coverage_complete: bool = True,
        coverage_reason: str = "complete",
    ) -> SignalPathResult:
        """Evaluate causally ordered observations and return path diagnostics.

        A trade tick supplies ordering when available. A bar can establish a
        touch only when one boundary is in its range; if both are touched in a
        single bar, ordering is unknowable and the result is ``AMBIGUOUS``.
        ``coverage_complete=False`` always yields ``UNKNOWN`` after recording
        excursions visible in the supplied observations.
        """
        events: list[tuple[int, int, str, TradeTick | CompletedBar]] = []
        incomplete_observation = False
        for trade in trades:
            if trade.occurred_at_ms >= self.start_at_ms:
                _validate_trade(trade)
                events.append((trade.occurred_at_ms, 0, "trade", trade))
        for bar in bars:
            _validate_bar(bar)
            if not bar.complete:
                incomplete_observation = True
            # A bar straddling the signal contains pre-signal prices and cannot
            # establish a causal first touch without underlying trades.
            if bar.opened_at_ms < self.start_at_ms < bar.closed_at_ms:
                incomplete_observation = True
                if coverage_reason == "complete":
                    coverage_reason = "bar_overlaps_signal_start"
            elif bar.complete and bar.opened_at_ms >= self.start_at_ms:
                events.append((bar.closed_at_ms, 1, "bar", bar))
        events.sort(key=lambda event: (event[0], event[1]))

        mfe = 0.0
        mae = 0.0
        first_touch: tuple[PathStatus, int, float] | None = None
        observed_until: int | None = None
        for timestamp, _, kind, event in events:
            observed_until = timestamp
            high, low = (event.price, event.price) if kind == "trade" else (event.high, event.low)
            mfe = max(mfe, (high - self.entry_price) if self.side == "buy" else (self.entry_price - low))
            mae = max(mae, (self.entry_price - low) if self.side == "buy" else (high - self.entry_price))
            touch = _touch(event, self.side, self.target_price, self.stop_price, kind)
            if touch is not None and first_touch is None:
                first_touch = (touch[0], timestamp, touch[1])
                if touch[0] in (PathStatus.TP_TOUCHED, PathStatus.SL_TOUCHED, PathStatus.AMBIGUOUS):
                    break

        if incomplete_observation:
            coverage_complete = False
            if coverage_reason == "complete":
                coverage_reason = "incomplete_bar"
        if not coverage_complete:
            status = PathStatus.UNKNOWN
            first_touch_at = first_touch_price = None
        elif first_touch is not None:
            status, first_touch_at, first_touch_price = first_touch
        elif timeout_at_ms is not None and timeout_at_ms >= self.start_at_ms and (
            observed_until is None or observed_until >= timeout_at_ms
        ):
            status, first_touch_at, first_touch_price = PathStatus.TIMEOUT, timeout_at_ms, None
        else:
            status, first_touch_at, first_touch_price = PathStatus.NO_TOUCH, None, None

        return SignalPathResult(
            signal_id=self.signal_id,
            side=self.side,
            entry_price=self.entry_price,
            target_price=self.target_price,
            stop_price=self.stop_price,
            start_at_ms=self.start_at_ms,
            status=status,
            first_touch_at_ms=first_touch_at,
            first_touch_price=first_touch_price,
            mfe_price=mfe,
            mae_price=-mae,
            mfe_bps=mfe / self.entry_price * 10_000,
            mae_bps=-mae / self.entry_price * 10_000,
            covered_until_ms=observed_until,
            coverage_complete=coverage_complete,
            coverage_reason=coverage_reason,
        )


class IncrementalSignalPath:
    """Bounded-memory live trade-tick path tracker.

    This is the streaming counterpart of :class:`SignalPathTracker`.  It keeps
    only extrema and the first boundary touch, so a high-volume public trade
    stream cannot grow an in-memory tick list without bound.
    """

    def __init__(self, tracker: SignalPathTracker) -> None:
        self.tracker = tracker
        self._last_at_ms = tracker.start_at_ms
        self._mfe = 0.0
        self._mae = 0.0
        self._terminal: SignalPathResult | None = None

    @property
    def terminal(self) -> bool:
        return self._terminal is not None

    def observe_trade(self, trade: TradeTick) -> SignalPathResult:
        _validate_trade(trade)
        if trade.occurred_at_ms < self._last_at_ms:
            raise ValueError("trade ticks must be observed in causal order")
        if self._terminal is not None:
            return self._terminal
        if trade.occurred_at_ms < self.tracker.start_at_ms:
            return self._result(PathStatus.NO_TOUCH, None, None, True, "complete")
        self._last_at_ms = trade.occurred_at_ms
        if self.tracker.side == "buy":
            self._mfe = max(self._mfe, trade.price - self.tracker.entry_price)
            self._mae = max(self._mae, self.tracker.entry_price - trade.price)
        else:
            self._mfe = max(self._mfe, self.tracker.entry_price - trade.price)
            self._mae = max(self._mae, trade.price - self.tracker.entry_price)
        touch = _touch(
            trade,
            self.tracker.side,
            self.tracker.target_price,
            self.tracker.stop_price,
            "trade",
        )
        if touch is None:
            return self._result(PathStatus.NO_TOUCH, None, None, True, "complete")
        self._terminal = self._result(touch[0], trade.occurred_at_ms, touch[1], True, "complete")
        return self._terminal

    def mark_unknown(self, occurred_at_ms: int, reason: str) -> SignalPathResult:
        if occurred_at_ms < self._last_at_ms or not reason:
            raise ValueError("unknown cutoff must be causal and have a reason")
        if self._terminal is None:
            self._last_at_ms = occurred_at_ms
            self._terminal = self._result(PathStatus.UNKNOWN, None, None, False, reason)
        return self._terminal

    def timeout(self, occurred_at_ms: int) -> SignalPathResult:
        if occurred_at_ms < self._last_at_ms:
            raise ValueError("timeout must be causal")
        if self._terminal is None:
            self._last_at_ms = occurred_at_ms
            self._terminal = self._result(PathStatus.TIMEOUT, occurred_at_ms, None, True, "complete")
        return self._terminal

    def _result(
        self,
        status: PathStatus,
        first_touch_at_ms: int | None,
        first_touch_price: float | None,
        coverage_complete: bool,
        coverage_reason: str,
    ) -> SignalPathResult:
        return SignalPathResult(
            signal_id=self.tracker.signal_id,
            side=self.tracker.side,
            entry_price=self.tracker.entry_price,
            target_price=self.tracker.target_price,
            stop_price=self.tracker.stop_price,
            start_at_ms=self.tracker.start_at_ms,
            status=status,
            first_touch_at_ms=first_touch_at_ms,
            first_touch_price=first_touch_price,
            mfe_price=self._mfe,
            mae_price=-self._mae,
            mfe_bps=self._mfe / self.tracker.entry_price * 10_000,
            mae_bps=-self._mae / self.tracker.entry_price * 10_000,
            covered_until_ms=self._last_at_ms,
            coverage_complete=coverage_complete,
            coverage_reason=coverage_reason,
        )


def _touch(
    event: TradeTick | CompletedBar,
    side: Side,
    target: float,
    stop: float,
    kind: str,
) -> tuple[PathStatus, float] | None:
    if kind == "trade":
        price = event.price  # type: ignore[union-attr]
        if (side == "buy" and price >= target) or (side == "sell" and price <= target):
            return PathStatus.TP_TOUCHED, price
        if (side == "buy" and price <= stop) or (side == "sell" and price >= stop):
            return PathStatus.SL_TOUCHED, price
        return None
    bar = event  # type: ignore[assignment]
    target_touched = (bar.high >= target) if side == "buy" else (bar.low <= target)
    stop_touched = (bar.low <= stop) if side == "buy" else (bar.high >= stop)
    if target_touched and stop_touched:
        return PathStatus.AMBIGUOUS, target if side == "buy" else stop
    if target_touched:
        return PathStatus.TP_TOUCHED, target
    if stop_touched:
        return PathStatus.SL_TOUCHED, stop
    return None


def _validate_trade(trade: TradeTick) -> None:
    if trade.occurred_at_ms < 0 or not math.isfinite(trade.price) or trade.price <= 0:
        raise ValueError("invalid trade tick")


def _validate_bar(bar: CompletedBar) -> None:
    if (
        bar.opened_at_ms < 0
        or bar.closed_at_ms <= bar.opened_at_ms
        or not all(math.isfinite(value) and value > 0 for value in (bar.high, bar.low))
        or bar.high < bar.low
    ):
        raise ValueError("invalid completed bar")
