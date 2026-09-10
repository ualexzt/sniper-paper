from __future__ import annotations

import json

import pytest

from sniper_paper.domain import LaneName, LevelClass, ProtocolError, Timeframe
from sniper_paper.protocol import PROTOCOL_PATH, load_protocol, load_protocol_dict


def test_protocol_file_loads_and_matches_expected_boundary() -> None:
    spec = load_protocol()

    assert spec.name == "paper_strategy_v1"
    assert spec.version == "v1"
    assert spec.safety_boundary.public_market_data_only is True
    assert spec.safety_boundary.separate_server_and_repository is True
    assert spec.safety_boundary.api_keys_allowed is False
    assert spec.safety_boundary.authenticated_orders_allowed is False
    assert spec.safety_boundary.paper_only is True
    assert spec.safety_boundary.simulated_fills_only is True
    assert spec.universe_policy.cadence == "once_daily"
    assert spec.universe_policy.timezone == "UTC"
    assert spec.universe_policy.anchor_time_utc == "00:00:00Z"
    assert spec.universe_policy.retroactive_additions_allowed is False
    assert spec.parameters["universe"]["max_symbols"] == 10
    assert spec.parameters["paper"]["latency_ms"] == 250
    assert spec.market_data_policy.required_timeframes == (
        Timeframe.MIN_1,
        Timeframe.MIN_5,
        Timeframe.MIN_15,
        Timeframe.H4,
    )
    assert [lane.name for lane in spec.lanes] == [
        LaneName.EARLY_TARGET_HUNT,
        LaneName.TERMINAL_LEVEL_BREAKOUT,
    ]
    assert [lane.target_timeframe for lane in spec.lanes] == [Timeframe.H4, Timeframe.MIN_15]
    assert [lane.level_class for lane in spec.lanes] == [
        LevelClass.HIGHER_TIMEFRAME_TARGET,
        LevelClass.FINAL_TARGET_BREAKOUT,
    ]
    assert all(lane.attempts_per_setup == 1 for lane in spec.lanes)
    assert all(lane.manual_target_changes_allowed is False for lane in spec.lanes)


def test_protocol_parser_is_strict_about_unknown_keys() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["unexpected"] = True

    with pytest.raises(ProtocolError, match="unexpected keys"):
        load_protocol_dict(payload)


def test_protocol_parser_rejects_lane_shape_drift() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["lanes"][0]["extra"] = "nope"

    with pytest.raises(ProtocolError, match="lane has unexpected keys"):
        load_protocol_dict(payload)


def test_v2_protocol_declares_immutable_versions_and_session_binding() -> None:
    v2_path = PROTOCOL_PATH.with_name("paper_strategy_v2.json")
    payload = json.loads(v2_path.read_text(encoding="utf-8"))

    assert payload["version"] == "v2.6.0"
    assert payload["versions"] == {
        "strategy": "v2.5.2-uniform-risk-brackets",
        "level": "digash_horizontal_levels_v2",
        "universe": "daily_universe_v2_shadow_metrics",
        "execution": "paper_execution_v2_dust_guard",
        "profit_protection": "paper_profit_protection_shadow_v2",
        "source_data_contract": "bybit_public_market_data_v2_complete_buckets",
    }
    assert payload["session_policy"] == {
        "session_id_format": "{utc_date}:{protocol_sha256}",
        "version_binding": "protocol_sha256",
        "new_session_on_version_change": True,
        "partial_day_bootstrap": "observation_only",
        "evaluation_eligibility": "complete_utc_day_after_anchor",
        "protocol_hash_required": True,
    }
    assert payload["horizontal_level_policy"] == {
        "runtime_source": "digash_horizontal_levels_v2",
        "completed_bars_only": True,
        "timeframes": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
        "history_bars": 1000,
        "extremum_search_period": 40,
        "right_exclusion_bars": 20,
        "merge_tolerance_endpoints_bp": {"1m": 20.0, "1d": 125.0},
        "pivot_rule": "digash_unique_centered_40_hypothesis_v1",
        "intermediate_tolerance_rule": "digash_tolerance_log_time_hypothesis_v1",
        "break_rule": "completed_close",
        "reactivation_allowed": False,
        "legacy_15m_4h_runtime_allowed": False,
    }
    assert payload["safety_boundary"]["paper_only"] is True
    assert payload["safety_boundary"]["authenticated_orders_allowed"] is False
    assert payload["metrics_observation_policy"]["trade_gate_enabled"] is False
    assert payload["metrics_observation_policy"]["liquidity_threshold"] is None
