"""Conservative public-data-only paper execution; never submits an order."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .storage import Journal


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class Quote:
    received_at_ms: int
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    bids: Mapping[float, float] | None = None
    asks: Mapping[float, float] | None = None

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.bid >= self.ask:
            raise ValueError("quote must have positive bid below ask")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("quote sizes cannot be negative")

    def displayed_size(self, side: Side, price: float) -> float:
        levels = self.bids if side is Side.LONG else self.asks
        if levels is not None:
            return max(0.0, float(levels.get(price, 0.0)))
        top_price = self.bid if side is Side.LONG else self.ask
        top_size = self.bid_size if side is Side.LONG else self.ask_size
        return top_size if math.isclose(price, top_price, rel_tol=0.0, abs_tol=1e-12) else 0.0


@dataclass(frozen=True)
class PaperSignal:
    signal_id: str
    occurred_at_ms: int
    symbol: str
    side: Side
    lane: str
    stop_price: float
    target_price: float


@dataclass
class PendingPaperOrder:
    order_id: str
    signal: PaperSignal
    created_at_ms: int
    activate_at_ms: int
    expires_at_ms: int
    entry_price: float
    quantity: float
    filled_qty: float = 0.0
    queue_ahead_qty: float = 0.0
    displayed_ahead_qty: float = 0.0
    status: str = "PENDING"

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.quantity - self.filled_qty)


@dataclass
class OpenPaperPosition:
    position_id: str
    signal: PaperSignal
    opened_at_ms: int
    entry_price: float
    quantity: float
    entry_fee: float
    mfe_bp: float = 0.0
    mae_bp: float = 0.0


class PaperExecutor:
    """One portfolio path with strict displayed-queue post-only entry."""

    def __init__(
        self,
        journal: Journal,
        *,
        equity: float = 10_000.0,
        risk_fraction: float = 0.001,
        daily_loss_fraction: float = 0.01,
        maker_fee_rate: float = 0.0002,
        taker_fee_rate: float = 0.00055,
        slippage_bp: float = 2.0,
        latency_ms: int = 250,
        entry_ttl_ms: int = 2_000,
    ) -> None:
        if equity <= 0 or not 0 < risk_fraction <= 0.01 or latency_ms < 0 or entry_ttl_ms <= 0:
            raise ValueError("invalid paper risk/execution configuration")
        self.journal = journal
        self.initial_equity = equity
        self.equity = equity + journal.realized_pnl()
        self.risk_fraction = risk_fraction
        self.daily_loss_fraction = daily_loss_fraction
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.slippage_bp = slippage_bp
        self.latency_ms = latency_ms
        self.entry_ttl_ms = entry_ttl_ms
        self.pending: PendingPaperOrder | None = None
        self.position: OpenPaperPosition | None = None

    def _signal_from_row(self, row: Mapping[str, Any]) -> PaperSignal:
        return PaperSignal(
            signal_id=str(row["signal_id"]),
            occurred_at_ms=int(row["occurred_at_ms"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            lane=str(row["lane"]),
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
        )

    def restore(self) -> bool:
        """Restore open position and in-flight entry from the SQLite journal."""
        restored = False
        row = self.journal.open_position_row()
        if row is not None:
            signal_row = self.journal.signal_row(str(row["signal_id"]))
            if signal_row is None:
                raise RuntimeError("open position has no signal")
            self.position = OpenPaperPosition(
                position_id=str(row["position_id"]),
                signal=self._signal_from_row(signal_row),
                opened_at_ms=int(row["opened_at_ms"]),
                entry_price=float(row["entry_price"]),
                quantity=float(row["quantity"]),
                entry_fee=float(row["entry_fee"]),
                mfe_bp=float(row["mfe_bp"]),
                mae_bp=float(row["mae_bp"]),
            )
            restored = True
        order = self.journal.paper_order_row()
        if order is not None:
            signal_row = self.journal.signal_row(str(order["signal_id"]))
            if signal_row is None:
                raise RuntimeError("paper order has no signal")
            self.pending = PendingPaperOrder(
                order_id=str(order["order_id"]),
                signal=self._signal_from_row(signal_row),
                created_at_ms=int(order["created_at_ms"]),
                activate_at_ms=int(order["created_at_ms"]) + self.latency_ms,
                expires_at_ms=int(order["created_at_ms"]) + self.latency_ms + self.entry_ttl_ms,
                entry_price=float(order["entry_price"]),
                quantity=float(order["quantity"]),
                filled_qty=float(order["filled_qty"]),
                queue_ahead_qty=float(order["queue_ahead_qty"]),
                displayed_ahead_qty=float(order["queue_ahead_qty"]),
                status=str(order["status"]),
            )
            restored = True
        return restored

    def submission_blocker(self, signal: PaperSignal) -> str | None:
        if self.pending is not None or self.position is not None:
            return "portfolio_busy"
        if self.journal.daily_realized_pnl(signal.occurred_at_ms) <= -self.initial_equity * self.daily_loss_fraction:
            return "daily_loss_limit"
        return None

    def submit(
        self,
        signal: PaperSignal,
        quote: Quote,
        *,
        qty_step: float = 0.0,
        min_order_qty: float = 0.0,
    ) -> bool:
        if self.submission_blocker(signal) is not None or quote.received_at_ms > signal.occurred_at_ms:
            return False
        _validate_bracket(signal)
        entry = quote.bid if signal.side is Side.LONG else quote.ask
        if signal.side is Side.LONG and not signal.stop_price < entry < signal.target_price:
            return False
        if signal.side is Side.SHORT and not signal.target_price < entry < signal.stop_price:
            return False
        direction = 1.0 if signal.side is Side.LONG else -1.0
        stop_exit = signal.stop_price * (1.0 - direction * self.slippage_bp / 10_000)
        risk_per_unit = abs(entry - stop_exit) + entry * self.maker_fee_rate + stop_exit * self.taker_fee_rate
        quantity = min(self.equity / entry, self.equity * self.risk_fraction / risk_per_unit)
        if qty_step > 0:
            quantity = math.floor(quantity / qty_step) * qty_step
        if quantity <= 0 or quantity < min_order_qty:
            return False
        pending = PendingPaperOrder(
            order_id=uuid.uuid4().hex,
            signal=signal,
            created_at_ms=signal.occurred_at_ms,
            activate_at_ms=signal.occurred_at_ms + self.latency_ms,
            expires_at_ms=signal.occurred_at_ms + self.latency_ms + self.entry_ttl_ms,
            entry_price=entry,
            quantity=quantity,
        )
        self.pending = pending
        self.journal.record_paper_order(
            {
                "order_id": pending.order_id,
                "signal_id": signal.signal_id,
                "created_at_ms": pending.created_at_ms,
                "activated_at_ms": None,
                "missed_at_ms": None,
                "symbol": signal.symbol,
                "side": signal.side.value,
                "lane": signal.lane,
                "entry_price": entry,
                "quantity": quantity,
                "filled_qty": 0.0,
                "queue_ahead_qty": 0.0,
                "status": "PENDING",
                "miss_reason": None,
                "last_quote_received_at_ms": quote.received_at_ms,
                "last_bid": quote.bid,
                "last_ask": quote.ask,
                "last_bid_size": quote.bid_size,
                "last_ask_size": quote.ask_size,
            }
        )
        return True

    def cancel_pending(
        self,
        reason: str = "market_stream_disconnected",
        now_ms: int | None = None,
    ) -> PaperSignal | None:
        pending = self.pending
        if pending is None:
            return None
        status = "CANCELLED" if pending.filled_qty > 0 else "MISSED"
        event_ms = pending.created_at_ms if now_ms is None else now_ms
        self.journal.update_paper_order(
            pending.order_id,
            status=status,
            missed_at_ms=event_ms,
            miss_reason=reason,
        )
        if pending.filled_qty <= 0:
            self.journal.update_signal_status(pending.signal.signal_id, "MISSED", reason)
        signal = pending.signal
        self.pending = None
        return signal

    def on_quote(self, symbol: str, quote: Quote) -> Mapping[str, Any] | None:
        pending_event = self._advance_pending(symbol, quote)
        position = self.position
        if position is None or position.signal.symbol != symbol:
            return pending_event
        executable = quote.bid if position.signal.side is Side.LONG else quote.ask
        direction = 1.0 if position.signal.side is Side.LONG else -1.0
        move_bp = direction * (executable / position.entry_price - 1.0) * 10_000
        position.mfe_bp = max(position.mfe_bp, move_bp)
        position.mae_bp = min(position.mae_bp, move_bp)
        self.journal.update_excursion(position.position_id, position.mfe_bp, position.mae_bp)
        stop_hit = (
            executable <= position.signal.stop_price
            if position.signal.side is Side.LONG
            else executable >= position.signal.stop_price
        )
        target_hit = (
            executable >= position.signal.target_price
            if position.signal.side is Side.LONG
            else executable <= position.signal.target_price
        )
        if stop_hit or target_hit:
            if self.pending is not None:
                self.cancel_pending("position_exit_cancelled_remainder", quote.received_at_ms)
            return self._close(quote, "SL" if stop_hit else "TP")
        return pending_event

    def _advance_pending(self, symbol: str, quote: Quote) -> Mapping[str, Any] | None:
        order = self.pending
        if order is None or order.signal.symbol != symbol:
            return None
        if quote.received_at_ms >= order.expires_at_ms:
            partial = order.filled_qty > 0
            self.cancel_pending("entry_ttl_remainder" if partial else "entry_ttl", quote.received_at_ms)
            return {"event": "PARTIAL_EXPIRED" if partial else "MISSED", "signal_id": order.signal.signal_id}
        displayed = quote.displayed_size(order.signal.side, order.entry_price)
        if order.status == "PENDING" and quote.received_at_ms >= order.activate_at_ms:
            post_only = order.entry_price < quote.ask if order.signal.side is Side.LONG else order.entry_price > quote.bid
            if not post_only or displayed <= 0:
                self.cancel_pending("post_only_or_level_invalid_at_arrival", quote.received_at_ms)
                return {"event": "MISSED", "signal_id": order.signal.signal_id}
            order.status = "ACTIVE"
            order.queue_ahead_qty = displayed
            order.displayed_ahead_qty = displayed
            self.journal.update_paper_order(
                order.order_id,
                activated_at_ms=quote.received_at_ms,
                status="ACTIVE",
                queue_ahead_qty=displayed,
            )
            return {"event": "ACTIVE", "order_id": order.order_id}
        if order.status in {"ACTIVE", "PARTIAL"}:
            if displayed > order.displayed_ahead_qty:
                order.queue_ahead_qty += displayed - order.displayed_ahead_qty
            order.displayed_ahead_qty = displayed
            self.journal.update_paper_order(
                order.order_id,
                queue_ahead_qty=order.queue_ahead_qty,
                last_quote_received_at_ms=quote.received_at_ms,
                last_bid=quote.bid,
                last_ask=quote.ask,
                last_bid_size=quote.bid_size,
                last_ask_size=quote.ask_size,
            )
        return None

    def on_trade(
        self,
        symbol: str,
        received_at_ms: int,
        taker_side: str,
        price: float,
        quantity: float,
    ) -> Mapping[str, Any] | None:
        order = self.pending
        if order is None or order.signal.symbol != symbol or order.status not in {"ACTIVE", "PARTIAL"}:
            return None
        if received_at_ms >= order.expires_at_ms:
            partial = order.filled_qty > 0
            self.cancel_pending("entry_ttl_remainder" if partial else "entry_ttl", received_at_ms)
            return {"event": "PARTIAL_EXPIRED" if partial else "MISSED", "signal_id": order.signal.signal_id}
        expected = "Sell" if order.signal.side is Side.LONG else "Buy"
        if taker_side != expected or not math.isclose(price, order.entry_price, rel_tol=0.0, abs_tol=1e-12):
            return None
        remaining_trade = max(0.0, quantity)
        queue_before = order.queue_ahead_qty
        consumed = min(order.queue_ahead_qty, remaining_trade)
        order.queue_ahead_qty = max(0.0, order.queue_ahead_qty - consumed)
        remaining_trade -= consumed
        remaining_before_fill = order.remaining_qty
        fill_qty = min(remaining_before_fill, remaining_trade)
        if fill_qty <= self._quantity_epsilon(queue_before, quantity, consumed, remaining_trade):
            self.journal.update_paper_order(order.order_id, queue_ahead_qty=order.queue_ahead_qty)
            return {"event": "QUEUE", "queue_consumed": consumed, "filled_qty": 0.0}
        self._fill(order, fill_qty, received_at_ms, remaining_before_fill)
        return {
            "event": "OPEN" if order.status == "FILLED" else "PARTIAL",
            "position_id": self.position.position_id if self.position else None,
            "queue_consumed": consumed,
            "filled_qty": fill_qty,
        }

    def _fill(
        self,
        order: PendingPaperOrder,
        fill_qty: float,
        received_at_ms: int,
        remaining_before_fill: float,
    ) -> None:
        order.filled_qty += fill_qty
        entry_fee = order.entry_price * fill_qty * self.maker_fee_rate
        if self.position is None:
            self.position = OpenPaperPosition(
                position_id=uuid.uuid4().hex,
                signal=order.signal,
                opened_at_ms=received_at_ms,
                entry_price=order.entry_price,
                quantity=fill_qty,
                entry_fee=entry_fee,
            )
            self.journal.open_position(
                {
                    "position_id": self.position.position_id,
                    "signal_id": order.signal.signal_id,
                    "opened_at_ms": received_at_ms,
                    "symbol": order.signal.symbol,
                    "side": order.signal.side.value,
                    "lane": order.signal.lane,
                    "entry_price": order.entry_price,
                    "stop_price": order.signal.stop_price,
                    "target_price": order.signal.target_price,
                    "quantity": fill_qty,
                    "entry_fee": entry_fee,
                    "status": "OPEN",
                }
            )
            self.journal.update_signal_status(order.signal.signal_id, "OPEN", "strict_queue_partial_fill")
        else:
            self.position.quantity += fill_qty
            self.position.entry_fee += entry_fee
            self.journal.update_open_position(
                self.position.position_id,
                quantity=self.position.quantity,
                entry_price=self.position.entry_price,
                entry_fee=self.position.entry_fee,
            )
        order.status = "FILLED" if order.remaining_qty <= self._quantity_epsilon(remaining_before_fill, fill_qty) else "PARTIAL"
        self.journal.update_paper_order(
            order.order_id,
            status=order.status,
            filled_qty=order.filled_qty,
            queue_ahead_qty=order.queue_ahead_qty,
        )
        if order.status == "FILLED":
            self.pending = None

    @staticmethod
    def _quantity_epsilon(*values: float) -> float:
        """Ignore subtraction residue, while retaining genuine small fills."""
        epsilon = max((math.ulp(abs(value)) for value in values if math.isfinite(value)), default=0.0)
        # A subtraction can accumulate one rounding unit from each operand;
        # the quantity step remains an execution constraint, not a reason to
        # discard a legitimate partial below an arbitrary absolute threshold.
        return 4.0 * epsilon

    def _close(self, quote: Quote, reason: str) -> Mapping[str, Any]:
        position = self.position
        if position is None:
            raise RuntimeError("no open paper position")
        raw_exit = quote.bid if position.signal.side is Side.LONG else quote.ask
        direction = 1.0 if position.signal.side is Side.LONG else -1.0
        exit_price = raw_exit * (1.0 - direction * self.slippage_bp / 10_000)
        gross = direction * (exit_price - position.entry_price) * position.quantity
        exit_fee = exit_price * position.quantity * self.taker_fee_rate
        net = gross - position.entry_fee - exit_fee
        outcome = {
            "event": "CLOSE",
            "position_id": position.position_id,
            "closed_at_ms": quote.received_at_ms,
            "exit_price": exit_price,
            "exit_fee": exit_fee,
            "exit_reason": reason,
            "gross_pnl": gross,
            "net_pnl": net,
        }
        self.journal.close_position(position.position_id, outcome)
        self.journal.update_signal_status(position.signal.signal_id, "CLOSED", reason)
        self.equity += net
        self.position = None
        return outcome


def _validate_bracket(signal: PaperSignal) -> None:
    valid = signal.stop_price < signal.target_price if signal.side is Side.LONG else signal.stop_price > signal.target_price
    if signal.stop_price <= 0 or signal.target_price <= 0 or not valid:
        raise ValueError("invalid signal bracket")
