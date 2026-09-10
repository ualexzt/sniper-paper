"""Paper-only counterfactual profit protection.

This module never changes the paper executor's SL/TP path.  It records what a
protected exit would have done using executable top-of-book prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .paper import OpenPaperPosition, Quote, Side


@dataclass(frozen=True, slots=True)
class ProfitProtectionConfig:
    enabled: bool = True
    activation_r: float = 1.0
    giveback_r: float = 0.5
    min_floor_r: float = 0.0
    delta_min_notional: float = 0.0
    delta_multiplier: float = 2.0
    delta_baseline_count: int = 5

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not math.isfinite(self.activation_r) or self.activation_r <= 0:
            raise ValueError("activation_r must be positive")
        if not math.isfinite(self.giveback_r) or self.giveback_r < 0:
            raise ValueError("giveback_r must be non-negative")
        if not math.isfinite(self.min_floor_r) or self.min_floor_r < 0:
            raise ValueError("min_floor_r must be non-negative")
        if not math.isfinite(self.delta_min_notional) or self.delta_min_notional < 0:
            raise ValueError("delta_min_notional must be non-negative")
        if not math.isfinite(self.delta_multiplier) or self.delta_multiplier < 0 or self.delta_baseline_count < 1:
            raise ValueError("invalid delta baseline configuration")


class ProfitProtection:
    """Stateful shadow evaluator for one open position."""

    def __init__(self, position: OpenPaperPosition, *, taker_fee_rate: float, slippage_bp: float,
                 config: ProfitProtectionConfig, terminal_modes: set[str] | None = None) -> None:
        if taker_fee_rate < 0 or slippage_bp < 0:
            raise ValueError("cost parameters cannot be negative")
        self.position_id = position.position_id
        self.side = position.signal.side
        self.entry_price = position.entry_price
        self.stop_price = position.signal.stop_price
        self.quantity = position.quantity
        self.entry_fee = position.entry_fee
        self.taker_fee_rate = taker_fee_rate
        self.slippage_bp = slippage_bp
        self.config = config
        self.risk_amount = max(0.0, -self._net_at_raw(position.signal.stop_price))
        direction = 1.0 if self.side is Side.LONG else -1.0
        mfe_raw = self.entry_price * (1.0 + direction * position.mfe_bp / 10_000)
        self.high_water_net = max(0.0, self._net_at_raw(mfe_raw))
        self.protected_floor = 0.0
        self.active = False
        self.last_executable = position.entry_price
        self.high_water_executable = position.entry_price
        self.last_bucket_closed_at_ms: int | None = None
        self._healthy_deltas: list[float] = []
        self._terminal_modes: set[str] = set(terminal_modes or ())
        if self.high_water_net >= self.config.activation_r * self.risk_amount:
            self.active = True
            self.protected_floor = max(self.config.min_floor_r * self.risk_amount,
                                       self.high_water_net - self.config.giveback_r * self.risk_amount)
        self._prior_low: float | None = None
        self._prior_high: float | None = None

    def sync_position(self, position: OpenPaperPosition) -> None:
        """Update quantity/cost after a partial fill without resetting guard state."""
        old_quantity = self.quantity
        self.quantity = position.quantity
        self.entry_fee = position.entry_fee
        if old_quantity > 0 and self.quantity >= old_quantity:
            ratio = self.quantity / old_quantity
            self.risk_amount = max(0.0, -self._net_at_raw(self.stop_price))
            self.high_water_net *= ratio
            self.protected_floor *= ratio

    def _exit_price(self, raw: float) -> float:
        direction = 1.0 if self.side is Side.LONG else -1.0
        return raw * (1.0 - direction * self.slippage_bp / 10_000)

    def _net_at_raw(self, raw: float) -> float:
        direction = 1.0 if self.side is Side.LONG else -1.0
        exit_price = self._exit_price(raw)
        return direction * (exit_price - self.entry_price) * self.quantity - self.entry_fee - exit_price * self.quantity * self.taker_fee_rate

    def _base(self, quote: Quote) -> dict[str, Any]:
        raw = quote.bid if self.side is Side.LONG else quote.ask
        net = self._net_at_raw(raw)
        self.high_water_net = max(self.high_water_net, net)
        if not self.active and net >= self.config.activation_r * self.risk_amount:
            self.active = True
        if self.active:
            floor = max(self.config.min_floor_r * self.risk_amount, self.high_water_net - self.config.giveback_r * self.risk_amount)
            self.protected_floor = max(self.protected_floor, floor)
        executable = self._exit_price(raw)
        structure_loss = (executable < self.high_water_executable if self.side is Side.LONG else executable > self.high_water_executable)
        if self.side is Side.LONG:
            self.high_water_executable = max(self.high_water_executable, executable)
        else:
            self.high_water_executable = min(self.high_water_executable, executable)
        self.last_executable = executable
        return {"net_pnl": net, "executable_exit_price": executable, "risk_amount": self.risk_amount,
                "active": self.active, "high_water_net": self.high_water_net,
                "protected_floor": self.protected_floor, "structure_loss": structure_loss}

    def on_quote(self, quote: Quote) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        item = self._base(quote)
        if not item["active"] or item["net_pnl"] > item["protected_floor"]:
            return []
        common = {"position_id": self.position_id, "occurred_at_ms": quote.received_at_ms,
                  "mode": "price_only", "decision": "SHADOW_EXIT", "reason": "protected_floor",
                  **item}
        rows = [] if "price_only" in self._terminal_modes else [common]
        self._terminal_modes.add("price_only")
        return rows

    def on_footprint(self, footprint: dict[str, Any], quote: Quote | None) -> list[dict[str, Any]]:
        if not self.config.enabled or quote is None or footprint.get("incomplete") or footprint.get("partial"):
            return []
        readiness = footprint.get("feed_readiness") or {}
        # A bucket is eligible only when the producer explicitly attests that
        # it is complete and the feed is out of reconnect/gap warmup.
        if not readiness.get("ready", False):
            return []
        if int(footprint.get("bucket_end_ms", 0)) <= (self.last_bucket_closed_at_ms or -1):
            return []
        self.last_bucket_closed_at_ms = int(footprint["bucket_end_ms"])
        delta = float(footprint.get("delta_notional", 0.0))
        baseline = sorted(abs(x) for x in self._healthy_deltas)[len(self._healthy_deltas) // 2] if self._healthy_deltas else 0.0
        threshold = max(self.config.delta_min_notional, self.config.delta_multiplier * baseline)
        adverse_delta = len(self._healthy_deltas) >= self.config.delta_baseline_count and (delta < -threshold if self.side is Side.LONG else delta > threshold)
        close = float(footprint.get("close", 0.0))
        price_loss = self._prior_low is not None and (close < self._prior_low if self.side is Side.LONG else close > self._prior_high)
        self._healthy_deltas.append(delta)
        self._healthy_deltas = self._healthy_deltas[-100:]
        self._prior_low, self._prior_high = float(footprint.get("low", close)), float(footprint.get("high", close))
        price_breach = self.active and self._net_at_raw(quote.bid if self.side is Side.LONG else quote.ask) <= self.protected_floor
        rows = self.on_quote(quote)
        if "price_plus_orderflow" in self._terminal_modes:
            return rows
        if not price_breach:
            return rows
        price_rows = list(rows)
        if not rows:
            raw = quote.bid if self.side is Side.LONG else quote.ask
            rows = [{"position_id": self.position_id, "occurred_at_ms": quote.received_at_ms,
                     "mode": "price_plus_orderflow", "decision": "WAIT_ORDERFLOW",
                     "reason": "protected_floor_without_complete_adverse_orderflow",
                     "net_pnl": self._net_at_raw(raw), "protected_floor": self.protected_floor,
                     "risk_amount": self.risk_amount, "active": self.active,
                     "high_water_net": self.high_water_net}]
        result = {**rows[0], "mode": "price_plus_orderflow", "bucket_start_ms": footprint.get("bucket_start_ms"),
                  "bucket_end_ms": footprint.get("bucket_end_ms"), "delta_notional": delta,
                  "adverse_aggressive_delta": adverse_delta, "price_structure_loss": price_loss,
                  "decision": "SHADOW_EXIT" if adverse_delta and price_loss else "WAIT_ORDERFLOW",
                  "reason": "adverse_delta_and_price_structure_loss" if adverse_delta and price_loss else "missing_adverse_delta_or_structure_loss"}
        if result["decision"] == "SHADOW_EXIT":
            self._terminal_modes.add("price_plus_orderflow")
        return [*price_rows, result]

    def on_baseline_close(self, outcome: dict[str, Any]) -> list[dict[str, Any]]:
        """Close each still-running shadow variant against the authoritative baseline."""
        rows = []
        for mode in ("price_only", "price_plus_orderflow"):
            if mode in self._terminal_modes:
                continue
            rows.append({"position_id": self.position_id, "occurred_at_ms": int(outcome["closed_at_ms"]),
                         "mode": mode, "decision": "BASELINE_EXIT", "reason": "baseline_" + str(outcome["exit_reason"]),
                         "exit_price": outcome.get("exit_price"), "net_pnl": outcome.get("net_pnl"),
                         "exit_reason": outcome.get("exit_reason")})
            self._terminal_modes.add(mode)
        return rows
