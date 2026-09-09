"""Causal receive-time market state used by replay and live paper mode."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Trade:
    symbol: str
    received_at_ms: int
    trade_id: str
    price: float
    quantity: float
    taker_side: str

    @property
    def signed_notional(self) -> float:
        sign = 1.0 if self.taker_side == "Buy" else -1.0
        return sign * self.price * self.quantity


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe_ms: int
    opened_at_ms: int
    closed_at_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    delta_notional: float
    trades: int


class Book:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.update_id: int | None = None
        self.sequence: int | None = None
        self.received_at_ms: int | None = None
        self.ready = False

    def invalidate(self) -> None:
        """Require a fresh snapshot before the book can be used again."""
        self.ready = False

    def apply(self, message: dict, received_at_ms: int) -> None:
        data = message.get("data") or {}
        if data.get("s") != self.symbol:
            raise ValueError("book symbol mismatch")
        kind = message.get("type")
        update_id = int(data["u"])
        sequence = int(data["seq"])
        if kind == "snapshot" or update_id == 1:
            self.bids.clear()
            self.asks.clear()
            self.ready = True
        elif kind != "delta" or not self.ready:
            self.invalidate()
            raise ValueError("delta before snapshot or invalid book message")
        elif self.update_id is not None and update_id <= self.update_id:
            self.invalidate()
            raise ValueError("non-increasing orderbook update id")
        elif self.sequence is not None and sequence <= self.sequence:
            self.invalidate()
            raise ValueError("non-increasing orderbook sequence")
        self._apply_side(self.bids, data.get("b", []))
        self._apply_side(self.asks, data.get("a", []))
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.invalidate()
            raise ValueError("crossed orderbook")
        self.update_id = update_id
        self.sequence = sequence
        self.received_at_ms = received_at_ms

    @staticmethod
    def _apply_side(side: dict[float, float], rows: Iterable[Iterable[str]]) -> None:
        for price_text, size_text in rows:
            price, size = float(price_text), float(size_text)
            if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size < 0:
                raise ValueError("invalid orderbook level")
            if size == 0:
                side.pop(price, None)
            else:
                side[price] = size

    @property
    def best_bid(self) -> float:
        if not self.ready or not self.bids:
            raise ValueError("book is not ready")
        return max(self.bids)

    @property
    def best_ask(self) -> float:
        if not self.ready or not self.asks:
            raise ValueError("book is not ready")
        return min(self.asks)

    def healthy(self, now_ms: int, max_age_ms: int, max_spread_bp: float) -> bool:
        if not self.ready or self.received_at_ms is None or not self.bids or not self.asks:
            return False
        spread_bp = (self.best_ask / self.best_bid - 1.0) * 10_000
        return now_ms - self.received_at_ms <= max_age_ms and spread_bp <= max_spread_bp

    def imbalance(self, depth: int = 5) -> float:
        if not self.ready or depth < 1:
            raise ValueError("book is not ready or depth is invalid")
        bid_size = sum(self.bids[p] for p in sorted(self.bids, reverse=True)[:depth])
        ask_size = sum(self.asks[p] for p in sorted(self.asks)[:depth])
        total = bid_size + ask_size
        return (bid_size - ask_size) / total if total else 0.0


class BarBuilder:
    """Emits only completed receive-time bars and never rewrites them."""

    def __init__(self, symbol: str, timeframe_ms: int) -> None:
        if timeframe_ms <= 0:
            raise ValueError("timeframe must be positive")
        self.symbol = symbol
        self.timeframe_ms = timeframe_ms
        self._bucket: int | None = None
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._open: float | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._close: float | None = None
        self._volume = 0.0
        self._delta_notional = 0.0
        self._trade_count = 0
        self._quarantine_first_bucket = False
        self._current: Bar | None = None

    def quarantine_first_bucket(self) -> None:
        """Suppress the current/next partial bucket after restart or reconnect."""
        self._bucket = None
        self._current = None
        self._reset_stats()
        self._quarantine_first_bucket = True

    def _reset_stats(self) -> None:
        self._open = self._high = self._low = self._close = None
        self._volume = self._delta_notional = 0.0
        self._trade_count = 0

    def add(self, trade: Trade) -> list[Bar]:
        if trade.symbol != self.symbol:
            raise ValueError("trade symbol mismatch")
        if trade.trade_id in self._seen_ids:
            return []
        bucket = trade.received_at_ms // self.timeframe_ms * self.timeframe_ms
        if self._bucket is not None and bucket < self._bucket:
            raise ValueError("receive time moved backwards")
        completed: list[Bar] = []
        if self._bucket is not None and bucket > self._bucket:
            if not self._quarantine_first_bucket:
                completed.append(self._finish())
            self._quarantine_first_bucket = False
            self._current = None
            self._reset_stats()
        self._bucket = bucket
        self._seen_ids.add(trade.trade_id)
        self._seen_order.append(trade.trade_id)
        if self._open is None:
            self._open = self._high = self._low = trade.price
        else:
            self._high = max(self._high, trade.price)
            self._low = min(self._low, trade.price)
        self._close = trade.price
        self._volume += trade.quantity
        self._delta_notional += trade.signed_notional
        self._trade_count += 1
        self._current = self._snapshot()
        while len(self._seen_order) > 100_000:
            self._seen_ids.discard(self._seen_order.popleft())
        return completed

    def current(self) -> Bar | None:
        """Return an immutable UI snapshot without completing or persisting the bar."""
        return self._current

    def _finish(self) -> Bar:
        if self._bucket is None or self._trade_count == 0:
            raise RuntimeError("cannot finish empty bar")
        return self._snapshot()

    def _snapshot(self) -> Bar:
        return Bar(
            symbol=self.symbol,
            timeframe_ms=self.timeframe_ms,
            opened_at_ms=self._bucket,
            closed_at_ms=self._bucket + self.timeframe_ms,
            open=self._open, high=self._high, low=self._low, close=self._close,
            volume=self._volume,
            delta_notional=self._delta_notional,
            trades=self._trade_count,
        )


@dataclass(frozen=True)
class FeedReadiness:
    connection_id: str | None
    received_at_ms: int | None
    gap_ms: int | None
    quarantined_until_ms: int | None
    quarantine_reason: str | None
    connected: bool
    gap_detected: bool
    ready: bool


class FeedClock:
    """Deterministic connection and gap readiness for public-feed consumers."""

    def __init__(self, gap_ms: int = 5_000, warmup_ms: int = 300_000) -> None:
        if gap_ms <= 0 or warmup_ms < 0:
            raise ValueError("gap_ms must be positive and warmup_ms must be non-negative")
        self.gap_ms = gap_ms
        self.warmup_ms = warmup_ms
        self.connection_id: str | None = None
        self.last_received_at_ms: int | None = None
        self.quarantined_until_ms: int | None = None
        self.quarantine_reason: str | None = None
        self._disconnected = False

    def disconnect(self, received_at_ms: int) -> FeedReadiness:
        if not isinstance(received_at_ms, int):
            raise TypeError("received_at_ms must be an integer")
        self._disconnected = True
        self.quarantined_until_ms = received_at_ms + self.warmup_ms
        self.quarantine_reason = "disconnect_warmup"
        self.last_received_at_ms = received_at_ms
        return FeedReadiness(
            connection_id=self.connection_id,
            received_at_ms=received_at_ms,
            gap_ms=None,
            quarantined_until_ms=self.quarantined_until_ms,
            quarantine_reason=self.quarantine_reason,
            connected=False,
            gap_detected=False,
            ready=False,
        )

    def observe(self, connection_id: str, received_at_ms: int, *, snapshot: bool = False) -> FeedReadiness:
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        if not isinstance(received_at_ms, int):
            raise TypeError("received_at_ms must be an integer")
        gap_ms = None if self.last_received_at_ms is None else received_at_ms - self.last_received_at_ms
        reconnect = self._disconnected or (self.connection_id is not None and connection_id != self.connection_id)
        gap_detected = gap_ms is not None and gap_ms > self.gap_ms
        if reconnect or gap_detected or snapshot:
            self.quarantined_until_ms = received_at_ms + self.warmup_ms
            if reconnect:
                self.quarantine_reason = "reconnect_warmup"
            elif gap_detected:
                self.quarantine_reason = "received_gap_warmup"
            else:
                self.quarantine_reason = "snapshot_warmup"
        self._disconnected = False
        self.connection_id = connection_id
        self.last_received_at_ms = received_at_ms
        ready = self.quarantined_until_ms is None or received_at_ms >= self.quarantined_until_ms
        return FeedReadiness(
            connection_id=self.connection_id,
            received_at_ms=received_at_ms,
            gap_ms=gap_ms,
            quarantined_until_ms=None if ready else self.quarantined_until_ms,
            quarantine_reason=None if ready else self.quarantine_reason,
            connected=True,
            gap_detected=gap_detected,
            ready=ready,
        )
