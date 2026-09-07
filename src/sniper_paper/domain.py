from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProtocolError(ValueError):
    """Raised when the frozen paper strategy protocol is malformed."""


class Timeframe(str, Enum):
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    H4 = "4h"


class LaneName(str, Enum):
    EARLY_TARGET_HUNT = "early_target_hunt"
    TERMINAL_LEVEL_BREAKOUT = "terminal_level_breakout"


class LevelClass(str, Enum):
    HIGHER_TIMEFRAME_TARGET = "higher_timeframe_target"
    FINAL_TARGET_BREAKOUT = "final_target_breakout"


class StrategyExecutionMode(str, Enum):
    PAPER = "paper"


class TradeDirection(str, Enum):
    BOTH = "both"


class ReEntryPolicy(str, Enum):
    DIAGNOSTIC_ONLY = "diagnostic_only"


@dataclass(frozen=True, slots=True)
class SafetyBoundary:
    public_market_data_only: bool
    separate_server_and_repository: bool
    api_keys_allowed: bool
    authenticated_orders_allowed: bool
    paper_only: bool
    simulated_fills_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_market_data_only": self.public_market_data_only,
            "separate_server_and_repository": self.separate_server_and_repository,
            "api_keys_allowed": self.api_keys_allowed,
            "authenticated_orders_allowed": self.authenticated_orders_allowed,
            "paper_only": self.paper_only,
            "simulated_fills_only": self.simulated_fills_only,
        }


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    cadence: str
    timezone: str
    anchor_time_utc: str
    retroactive_additions_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "cadence": self.cadence,
            "timezone": self.timezone,
            "anchor_time_utc": self.anchor_time_utc,
            "retroactive_additions_allowed": self.retroactive_additions_allowed,
        }


@dataclass(frozen=True, slots=True)
class MarketDataPolicy:
    required_timeframes: tuple[Timeframe, ...]
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_timeframes": [timeframe.value for timeframe in self.required_timeframes],
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    mode: StrategyExecutionMode
    one_attempt_per_setup: bool
    deterministic_take_profit: bool
    deterministic_stop_loss: bool
    include_fees: bool
    include_latency: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "one_attempt_per_setup": self.one_attempt_per_setup,
            "deterministic_take_profit": self.deterministic_take_profit,
            "deterministic_stop_loss": self.deterministic_stop_loss,
            "include_fees": self.include_fees,
            "include_latency": self.include_latency,
        }


@dataclass(frozen=True, slots=True)
class LaneConfig:
    name: LaneName
    target_timeframe: Timeframe
    level_class: LevelClass
    exit_mode: str
    direction: TradeDirection
    attempts_per_setup: int
    manual_target_changes_allowed: bool
    re_entry_policy: ReEntryPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "target_timeframe": self.target_timeframe.value,
            "level_class": self.level_class.value,
            "exit_mode": self.exit_mode,
            "direction": self.direction.value,
            "attempts_per_setup": self.attempts_per_setup,
            "manual_target_changes_allowed": self.manual_target_changes_allowed,
            "re_entry_policy": self.re_entry_policy.value,
        }


@dataclass(frozen=True, slots=True)
class ProtocolSpec:
    name: str
    version: str
    safety_boundary: SafetyBoundary
    universe_policy: UniversePolicy
    market_data_policy: MarketDataPolicy
    execution_policy: ExecutionPolicy
    parameters: Mapping[str, Mapping[str, int | float]]
    lanes: tuple[LaneConfig, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "safety_boundary": self.safety_boundary.to_dict(),
            "universe_policy": self.universe_policy.to_dict(),
            "market_data_policy": self.market_data_policy.to_dict(),
            "execution_policy": self.execution_policy.to_dict(),
            "parameters": {group: dict(values) for group, values in self.parameters.items()},
            "lanes": [lane.to_dict() for lane in self.lanes],
        }
