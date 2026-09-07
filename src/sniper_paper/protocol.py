from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .domain import (
    ExecutionPolicy,
    LaneConfig,
    LaneName,
    LevelClass,
    MarketDataPolicy,
    ProtocolError,
    ProtocolSpec,
    ReEntryPolicy,
    SafetyBoundary,
    StrategyExecutionMode,
    Timeframe,
    TradeDirection,
    UniversePolicy,
)

_REPO_PROTOCOL = Path(__file__).resolve().parents[2] / "paper_strategy_v1.json"
_CWD_PROTOCOL = Path.cwd() / "paper_strategy_v1.json"
PROTOCOL_PATH = _REPO_PROTOCOL if _REPO_PROTOCOL.exists() else _CWD_PROTOCOL


def load_protocol(path: Path | None = None) -> ProtocolSpec:
    protocol_path = path or PROTOCOL_PATH
    return load_protocol_dict(json.loads(protocol_path.read_text(encoding="utf-8")))


def protocol_sha256(path: Path | None = None) -> str:
    protocol_path = path or PROTOCOL_PATH
    return hashlib.sha256(protocol_path.read_bytes()).hexdigest()


def load_protocol_dict(data: Mapping[str, Any]) -> ProtocolSpec:
    _expect_keys(
        data,
        {
            "name",
            "version",
            "safety_boundary",
            "universe_policy",
            "market_data_policy",
            "execution_policy",
            "parameters",
            "lanes",
        },
        "top-level protocol",
    )

    name = _require_str(data["name"], "name")
    version = _require_str(data["version"], "version")
    safety_boundary = _parse_safety_boundary(_require_mapping(data["safety_boundary"], "safety_boundary"))
    universe_policy = _parse_universe_policy(_require_mapping(data["universe_policy"], "universe_policy"))
    market_data_policy = _parse_market_data_policy(_require_mapping(data["market_data_policy"], "market_data_policy"))
    execution_policy = _parse_execution_policy(_require_mapping(data["execution_policy"], "execution_policy"))
    parameters = _parse_parameters(_require_mapping(data["parameters"], "parameters"))
    lanes = tuple(
        _parse_lane(_require_mapping(item, f"lanes[{index}]"))
        for index, item in enumerate(_require_sequence(data["lanes"], "lanes"))
    )

    if not lanes:
        raise ProtocolError("lanes must contain at least one entry")

    return ProtocolSpec(
        name=name,
        version=version,
        safety_boundary=safety_boundary,
        universe_policy=universe_policy,
        market_data_policy=market_data_policy,
        execution_policy=execution_policy,
        parameters=parameters,
        lanes=lanes,
    )


def _parse_safety_boundary(data: Mapping[str, Any]) -> SafetyBoundary:
    _expect_keys(
        data,
        {
            "public_market_data_only",
            "separate_server_and_repository",
            "api_keys_allowed",
            "authenticated_orders_allowed",
            "paper_only",
            "simulated_fills_only",
        },
        "safety_boundary",
    )
    return SafetyBoundary(
        public_market_data_only=_require_bool(
            data["public_market_data_only"], "safety_boundary.public_market_data_only"
        ),
        separate_server_and_repository=_require_bool(
            data["separate_server_and_repository"], "safety_boundary.separate_server_and_repository"
        ),
        api_keys_allowed=_require_bool(data["api_keys_allowed"], "safety_boundary.api_keys_allowed"),
        authenticated_orders_allowed=_require_bool(
            data["authenticated_orders_allowed"], "safety_boundary.authenticated_orders_allowed"
        ),
        paper_only=_require_bool(data["paper_only"], "safety_boundary.paper_only"),
        simulated_fills_only=_require_bool(data["simulated_fills_only"], "safety_boundary.simulated_fills_only"),
    )


def _parse_universe_policy(data: Mapping[str, Any]) -> UniversePolicy:
    _expect_keys(
        data,
        {
            "cadence",
            "timezone",
            "anchor_time_utc",
            "retroactive_additions_allowed",
        },
        "universe_policy",
    )
    cadence = _require_str(data["cadence"], "universe_policy.cadence")
    timezone = _require_str(data["timezone"], "universe_policy.timezone")
    anchor_time_utc = _require_str(data["anchor_time_utc"], "universe_policy.anchor_time_utc")
    retroactive = _require_bool(data["retroactive_additions_allowed"], "universe_policy.retroactive_additions_allowed")
    return UniversePolicy(
        cadence=cadence,
        timezone=timezone,
        anchor_time_utc=anchor_time_utc,
        retroactive_additions_allowed=retroactive,
    )


def _parse_market_data_policy(data: Mapping[str, Any]) -> MarketDataPolicy:
    _expect_keys(data, {"required_timeframes", "sources"}, "market_data_policy")
    timeframe_values = [
        _require_timeframe(value, f"market_data_policy.required_timeframes[{index}]")
        for index, value in enumerate(
            _require_sequence(data["required_timeframes"], "market_data_policy.required_timeframes")
        )
    ]
    sources = tuple(
        _require_str(value, f"market_data_policy.sources[{index}]")
        for index, value in enumerate(_require_sequence(data["sources"], "market_data_policy.sources"))
    )
    return MarketDataPolicy(required_timeframes=tuple(timeframe_values), sources=sources)


def _parse_execution_policy(data: Mapping[str, Any]) -> ExecutionPolicy:
    _expect_keys(
        data,
        {
            "mode",
            "one_attempt_per_setup",
            "deterministic_take_profit",
            "deterministic_stop_loss",
            "include_fees",
            "include_latency",
        },
        "execution_policy",
    )
    return ExecutionPolicy(
        mode=_require_enum(StrategyExecutionMode, data["mode"], "execution_policy.mode"),
        one_attempt_per_setup=_require_bool(data["one_attempt_per_setup"], "execution_policy.one_attempt_per_setup"),
        deterministic_take_profit=_require_bool(
            data["deterministic_take_profit"], "execution_policy.deterministic_take_profit"
        ),
        deterministic_stop_loss=_require_bool(
            data["deterministic_stop_loss"], "execution_policy.deterministic_stop_loss"
        ),
        include_fees=_require_bool(data["include_fees"], "execution_policy.include_fees"),
        include_latency=_require_bool(data["include_latency"], "execution_policy.include_latency"),
    )


def _parse_lane(data: Mapping[str, Any]) -> LaneConfig:
    _expect_keys(
        data,
        {
            "name",
            "target_timeframe",
            "level_class",
            "exit_mode",
            "direction",
            "attempts_per_setup",
            "manual_target_changes_allowed",
            "re_entry_policy",
        },
        "lane",
    )
    return LaneConfig(
        name=_require_enum(LaneName, data["name"], "lane.name"),
        target_timeframe=_require_timeframe(data["target_timeframe"], "lane.target_timeframe"),
        level_class=_require_enum(LevelClass, data["level_class"], "lane.level_class"),
        exit_mode=_require_str(data["exit_mode"], "lane.exit_mode"),
        direction=_require_enum(TradeDirection, data["direction"], "lane.direction"),
        attempts_per_setup=_require_int(data["attempts_per_setup"], "lane.attempts_per_setup"),
        manual_target_changes_allowed=_require_bool(
            data["manual_target_changes_allowed"], "lane.manual_target_changes_allowed"
        ),
        re_entry_policy=_require_enum(ReEntryPolicy, data["re_entry_policy"], "lane.re_entry_policy"),
    )


def _parse_parameters(data: Mapping[str, Any]) -> Mapping[str, Mapping[str, int | float]]:
    expected = {
        "universe": {"max_symbols", "candidate_pool_multiplier", "max_spread_bps", "min_depth_notional_top5"},
        "data_quality": {"orderbook_depth", "max_book_age_ms", "warmup_minutes_after_snapshot"},
        "strategy": {
            "trend_lookback_4h",
            "min_trend_move_pct",
            "consolidation_bars_5m",
            "max_consolidation_width_bp",
            "trigger_lookback_1m",
            "min_book_imbalance",
            "min_reward_risk",
            "terminal_reward_risk",
            "stop_buffer_bp",
        },
        "paper": {
            "initial_equity",
            "risk_fraction",
            "daily_loss_fraction",
            "taker_fee_rate",
            "slippage_bp",
            "latency_ms",
        },
    }
    _expect_keys(data, set(expected), "parameters")
    result: dict[str, dict[str, int | float]] = {}
    for group, keys in expected.items():
        values = _require_mapping(data[group], f"parameters.{group}")
        _expect_keys(values, keys, f"parameters.{group}")
        parsed: dict[str, int | float] = {}
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ProtocolError(f"parameters.{group}.{key} must be numeric")
            if value < 0:
                raise ProtocolError(f"parameters.{group}.{key} must be non-negative")
            parsed[key] = value
        result[group] = parsed
    if int(result["universe"]["max_symbols"]) < 1:
        raise ProtocolError("parameters.universe.max_symbols must be positive")
    if not 0 < float(result["paper"]["risk_fraction"]) <= 0.01:
        raise ProtocolError("parameters.paper.risk_fraction must be in (0, 0.01]")
    if not 0 < float(result["paper"]["daily_loss_fraction"]) <= 0.05:
        raise ProtocolError("parameters.paper.daily_loss_fraction must be in (0, 0.05]")
    return result


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{context} must be an object")
    return value


def _require_sequence(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{context} must be an array")
    return value


def _require_str(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{context} must be a string")
    if not value:
        raise ProtocolError(f"{context} must not be empty")
    return value


def _require_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{context} must be a boolean")
    return value


def _require_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{context} must be an integer")
    return value


def _require_enum(enum_type: type[Any], value: Any, context: str) -> Any:
    text = _require_str(value, context)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise ProtocolError(f"{context} must be one of {[member.value for member in enum_type]}") from exc


def _require_timeframe(value: Any, context: str) -> Timeframe:
    return _require_enum(Timeframe, value, context)


def _expect_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        parts: list[str] = []
        if missing:
            parts.append(f"missing keys {missing}")
        if extra:
            parts.append(f"unexpected keys {extra}")
        raise ProtocolError(f"{context} has {' and '.join(parts)}")
