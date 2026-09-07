"""Deterministic executable-quote paper execution; never submits an order."""

from __future__ import annotations

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

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.bid >= self.ask:
            raise ValueError("quote must have positive bid below ask")


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
    """Single-position portfolio with taker entry/exit at executable quotes."""

    def __init__(
        self,
        journal: Journal,
        *,
        equity: float = 10_000.0,
        risk_fraction: float = 0.001,
        daily_loss_fraction: float = 0.01,
        taker_fee_rate: float = 0.00055,
        slippage_bp: float = 2.0,
        latency_ms: int = 250,
    ) -> None:
        if equity <= 0 or not 0 < risk_fraction <= 0.01:
            raise ValueError("invalid paper risk configuration")
        self.journal = journal
        self.initial_equity = equity
        self.equity = equity + journal.realized_pnl()
        self.risk_fraction = risk_fraction
        self.daily_loss_fraction = daily_loss_fraction
        self.taker_fee_rate = taker_fee_rate
        self.slippage_bp = slippage_bp
        self.latency_ms = latency_ms
        self.pending: PaperSignal | None = None
        self.position: OpenPaperPosition | None = None

    def restore(self) -> bool:
        """Restore the one open paper position after a process restart."""
        row = self.journal.open_position_row()
        if row is None:
            return False
        signal_row = self.journal.signal_row(str(row["signal_id"]))
        if signal_row is None:
            raise RuntimeError("open position has no signal")
        signal = PaperSignal(
            signal_id=str(signal_row["signal_id"]),
            occurred_at_ms=int(signal_row["occurred_at_ms"]),
            symbol=str(signal_row["symbol"]),
            side=Side(str(signal_row["side"])),
            lane=str(signal_row["lane"]),
            stop_price=float(signal_row["stop_price"]),
            target_price=float(signal_row["target_price"]),
        )
        self.position = OpenPaperPosition(
            position_id=str(row["position_id"]),
            signal=signal,
            opened_at_ms=int(row["opened_at_ms"]),
            entry_price=float(row["entry_price"]),
            quantity=float(row["quantity"]),
            entry_fee=float(row["entry_fee"]),
            mfe_bp=float(row["mfe_bp"]),
            mae_bp=float(row["mae_bp"]),
        )
        return True

    def submit(self, signal: PaperSignal) -> bool:
        if self.submission_blocker(signal) is not None:
            return False
        _validate_bracket(signal)
        self.pending = signal
        return True

    def submission_blocker(self, signal: PaperSignal) -> str | None:
        if self.pending is not None or self.position is not None:
            return "portfolio_busy"
        if self.journal.daily_realized_pnl(signal.occurred_at_ms) <= -self.initial_equity * self.daily_loss_fraction:
            return "daily_loss_limit"
        return None

    def cancel_pending(self) -> PaperSignal | None:
        pending, self.pending = self.pending, None
        return pending

    def on_quote(self, symbol: str, quote: Quote) -> Mapping[str, Any] | None:
        if (
            self.pending
            and self.pending.symbol == symbol
            and quote.received_at_ms >= self.pending.occurred_at_ms + self.latency_ms
        ):
            self._open(self.pending, quote)
            self.pending = None
            return {"event": "OPEN", "position_id": self.position.position_id}

        position = self.position
        if position is None or position.signal.symbol != symbol:
            return None
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
        if not stop_hit and not target_hit:
            return None
        reason = "SL" if stop_hit else "TP"
        return self._close(quote, reason)

    def _open(self, signal: PaperSignal, quote: Quote) -> None:
        raw_entry = quote.ask if signal.side is Side.LONG else quote.bid
        direction = 1.0 if signal.side is Side.LONG else -1.0
        entry = raw_entry * (1.0 + direction * self.slippage_bp / 10_000)
        if signal.side is Side.LONG and not signal.stop_price < entry < signal.target_price:
            raise ValueError("latency moved long entry outside its frozen bracket")
        if signal.side is Side.SHORT and not signal.target_price < entry < signal.stop_price:
            raise ValueError("latency moved short entry outside its frozen bracket")
        stop_exit = signal.stop_price * (1.0 - direction * self.slippage_bp / 10_000)
        risk_per_unit = abs(entry - stop_exit) + entry * self.taker_fee_rate + stop_exit * self.taker_fee_rate
        if risk_per_unit <= 0:
            raise ValueError("entry and stop must differ")
        quantity = min(self.equity / entry, self.equity * self.risk_fraction / risk_per_unit)
        entry_fee = entry * quantity * self.taker_fee_rate
        position = OpenPaperPosition(
            position_id=uuid.uuid4().hex,
            signal=signal,
            opened_at_ms=quote.received_at_ms,
            entry_price=entry,
            quantity=quantity,
            entry_fee=entry_fee,
        )
        self.position = position
        self.journal.open_position(
            {
                "position_id": position.position_id,
                "signal_id": signal.signal_id,
                "opened_at_ms": quote.received_at_ms,
                "symbol": signal.symbol,
                "side": signal.side.value,
                "lane": signal.lane,
                "entry_price": entry,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
                "quantity": quantity,
                "entry_fee": entry_fee,
                "status": "OPEN",
            }
        )

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
        self.equity += net
        self.position = None
        return outcome


def _validate_bracket(signal: PaperSignal) -> None:
    if signal.side is Side.LONG:
        valid = signal.stop_price < signal.target_price
    else:
        valid = signal.stop_price > signal.target_price
    if signal.stop_price <= 0 or signal.target_price <= 0 or not valid:
        raise ValueError("invalid signal bracket")
