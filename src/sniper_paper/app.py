"""Live public-market orchestration for forward paper evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from .bybit_public import BybitPublicClient
from .levels import DIGASH_LEVEL_TIMEFRAMES, DIGASH_LEVEL_VERSION, Level, LevelSide
from .liquidity import LiquidityImpact, calculate_liquidity_impact
from .market import Bar, BarBuilder, Book, Trade
from .metrics import MetricResult, btc_correlation, dollar_volume, natr_5m_14, signed_price_change, volume_splash
from .orderflow import DomTracker, Footprint
from .paper import PaperExecutor, PaperSignal, Quote
from .paper import Side as PaperSide
from .shadow_levels import GeometryConfig, build_reference_levels
from .shadow_orderflow import DensityWallEvidence, ShadowOrderflowEvaluator, ShadowStatus
from .shadow_setups import RetestReclaimShadowEvaluator, ShadowSetupStatus
from .signal_path import IncrementalSignalPath, PathStatus, SignalPathResult, SignalPathTracker, TradeTick
from .storage import Journal
from .strategy import (
    apply_level_breaks,
    current_display_levels,
)
from .strategy_v2 import (
    OrderflowFrame,
    StrategyV2Evaluator,
)
from .strategy_v2 import (
    StrategyDecision as StrategyDecisionV2,
)
from .stream import run_public_stream
from .universe import DailyUniverseSelector
from .web import start_dashboard

TIMEFRAMES = {
    "15s": 15_000, "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
BYBIT_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
DIGASH_TIMEFRAMES = tuple(timeframe for timeframe in BYBIT_INTERVALS if timeframe in DIGASH_LEVEL_TIMEFRAMES)
# The level engines need enough completed history to reconstruct causal pivots
# after a restart.  Keep this independent from the chart's display window.
HISTORY_LIMIT = 1_000
BAR_RETENTION = 1_000
_REPO_V2_PROTOCOL = Path(__file__).resolve().parents[2] / "paper_strategy_v2.json"
_CWD_V2_PROTOCOL = Path.cwd() / "paper_strategy_v2.json"
V2_PROTOCOL_PATH = _REPO_V2_PROTOCOL if _REPO_V2_PROTOCOL.exists() else _CWD_V2_PROTOCOL


@dataclass
class SymbolState:
    symbol: str
    tick_size: float = 0.01
    qty_step: float = 0.0
    min_order_qty: float = 0.0
    book: Book = field(init=False)
    bars: dict[str, list[Bar]] = field(default_factory=lambda: {name: [] for name in TIMEFRAMES})
    history_diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)
    metric_snapshot: dict[str, Any] = field(default_factory=dict)
    builders: dict[str, BarBuilder] = field(init=False)
    levels: list[Level] = field(default_factory=list)
    snapshot_received_at_ms: int | None = None
    last_orderflow: dict[str, Any] | None = None
    last_dom_events: list[dict[str, Any]] = field(default_factory=list)
    density_walls: dict[tuple[str, float], dict[str, Any]] = field(default_factory=dict)
    shadow_active: bool = False
    last_blocker: str | None = None
    orderflow_ready: bool = False
    recorded_decisions: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.book = Book(self.symbol)
        self.builders = {name: BarBuilder(self.symbol, milliseconds) for name, milliseconds in TIMEFRAMES.items()}


class PaperApp:
    def __init__(self, journal: Journal, client: BybitPublicClient | None = None) -> None:
        self.journal = journal
        self.client = client or BybitPublicClient()
        orphaned = self.journal.reconcile_orphaned_triggers()
        self.protocol_data = json.loads(V2_PROTOCOL_PATH.read_text(encoding="utf-8"))
        self.protocol_hash = hashlib.sha256(V2_PROTOCOL_PATH.read_bytes()).hexdigest()
        self.version_info = {str(key): str(value) for key, value in self.protocol_data["versions"].items()}
        self.level_version = self.version_info["level"]
        if self.level_version != DIGASH_LEVEL_VERSION:
            raise ValueError("protocol level version does not match the runtime Digash detector")
        self.session_id = "unbound"
        signal_path_policy = self.protocol_data.get("signal_path_policy", {})
        self.signal_path_horizon_ms = int(signal_path_policy.get("horizon_ms", 86_400_000))
        self.signal_paths: dict[str, tuple[str, IncrementalSignalPath]] = {}
        orphaned_paths = self._reconcile_orphaned_signal_paths()
        shadow_start_key = f"shadow_started_at_ms:{self.protocol_hash}"
        shadow_start = self.journal.get_meta(shadow_start_key)
        if shadow_start is None:
            shadow_start = str(_now_ms())
            self.journal.set_meta(shadow_start_key, shadow_start)
        self.shadow_started_at_ms = int(shadow_start)
        params = self.protocol_data["parameters"]
        self.selector = DailyUniverseSelector(
            self.client,
            max_symbols=int(params["universe"]["max_symbols"]),
            candidate_pool_multiplier=int(params["universe"]["candidate_pool_multiplier"]),
            max_spread_bps=str(params["universe"]["max_spread_bps"]),
            min_depth_notional_top5=str(params["universe"]["min_depth_notional_top5"]),
        )
        self.strategies: dict[str, StrategyV2Evaluator] = {}
        self.shadow_retests: dict[str, RetestReclaimShadowEvaluator] = {}
        self.shadow_orderflow: dict[str, ShadowOrderflowEvaluator] = {}
        self.restored_setup_ids = self.journal.attempted_setup_ids()
        self.executor = PaperExecutor(
            journal,
            equity=float(params["paper"]["initial_equity"]),
            risk_fraction=float(params["paper"]["risk_fraction"]),
            daily_loss_fraction=float(params["paper"]["daily_loss_fraction"]),
            taker_fee_rate=float(params["paper"]["taker_fee_rate"]),
            maker_fee_rate=float(params["paper"].get("maker_fee_rate", 0.0002)),
            slippage_bp=float(params["paper"]["slippage_bp"]),
            latency_ms=int(params["paper"]["latency_ms"]),
            entry_ttl_ms=int(self.protocol_data["execution_policy"]["entry_ttl_ms"]),
        )
        self.executor.restore()
        self.max_book_age_ms = int(params["data_quality"]["max_book_age_ms"])
        self.warmup_ms = int(params["data_quality"]["warmup_minutes_after_snapshot"]) * 60_000
        lifecycle = params["level_lifecycle"]
        self.level_break_buffer_ticks = int(lifecycle["break_buffer_ticks"])
        self.level_break_fast_timeframe = str(lifecycle["fast_timeframe"])
        self.level_break_fast_closes = int(lifecycle["fast_confirming_closes"])
        self.level_break_source_closes = int(lifecycle["source_timeframe_confirming_closes"])
        self.states: dict[str, SymbolState] = {}
        self.footprint: Footprint | None = None
        self.dom: DomTracker | None = None
        self.connection_id = "disconnected"
        self.stream_connected = False
        self.evaluation_eligible = False
        if orphaned:
            self.journal.event(_now_ms(), "WARN", "RESTART_RECONCILE", f"marked {orphaned} pending triggers MISSED")
        if orphaned_paths:
            self.journal.event(
                _now_ms(),
                "WARN",
                "SIGNAL_PATH_RESTART",
                f"marked {orphaned_paths} signal paths UNKNOWN after restart",
            )

    async def run_forever(self) -> None:
        self.journal.set_meta("stream_state", "disconnected")
        self.journal.event(_now_ms(), "INFO", "START", "paper service started", {"protocol_hash": self.protocol_hash})
        while True:
            try:
                await self._run_daily_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the long-running paper service observable
                self.journal.event(_now_ms(), "ERROR", "SESSION", str(exc), {"type": type(exc).__name__})
                await asyncio.sleep(15)

    async def _run_daily_session(self) -> None:
        now = datetime.now(UTC)
        day = now.date().isoformat()
        self.session_id = f"{day}:{self.protocol_hash}"
        self.journal.set_meta("current_session_id", self.session_id)
        self.journal.set_meta("current_protocol_hash", self.protocol_hash)
        self.journal.set_meta("current_versions_json", json.dumps(self.version_info, sort_keys=True))
        session_deadline = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        existing = self.journal.universe_for_date(day)
        symbol_meta: dict[str, dict[str, Any]] = {}
        if existing:
            symbols = [str(row["symbol"]) for row in existing]
            for row in existing:
                metrics = json.loads(str(row.get("metrics_json", "{}")))
                symbol_meta[str(row["symbol"])] = metrics
            run = self.journal.universe_run_for_date(day)
            same_protocol = bool(run and run.get("protocol_hash") == self.protocol_hash)
            self.evaluation_eligible = bool(
                same_protocol and run and run["source"].get("evaluation_eligible", False)
            )
            self.journal.event(_now_ms(), "INFO", "UNIVERSE_RESTORE", f"restored {len(symbols)} daily symbols")
            if run and not same_protocol:
                self.journal.event(
                    _now_ms(),
                    "WARN",
                    "PROTOCOL_CHANGED_MIDDAY",
                    "restored frozen daily symbols in observation-only mode",
                    {"prior_protocol_hash": run.get("protocol_hash"), "protocol_hash": self.protocol_hash},
                )
        else:
            snapshot = await asyncio.to_thread(self.selector.build_snapshot, now)
            symbols = [item.symbol for item in snapshot.selected]
            if not symbols:
                raise RuntimeError("daily selector returned no eligible symbols")
            payload = snapshot.to_dict()
            seconds_after_midnight = (now - datetime.combine(now.date(), datetime.min.time(), UTC)).total_seconds()
            payload["selection_mode"] = "utc_anchor" if seconds_after_midnight <= 300 else "partial_day_bootstrap"
            payload["evaluation_eligible"] = seconds_after_midnight <= 300
            payload["session_id"] = self.session_id
            payload["versions"] = self.version_info
            payload["protocol_hash"] = self.protocol_hash
            self.evaluation_eligible = bool(payload["evaluation_eligible"])
            members = [
                {
                    "symbol": item.symbol,
                    "rank": item.rank,
                    "selected": True,
                    "reason": "selected",
                    "metrics": item.to_dict(),
                }
                for item in snapshot.selected
            ] + [
                {
                    "symbol": item.symbol,
                    "rank": len(snapshot.selected) + index,
                    "selected": False,
                    "reason": ",".join(item.reasons),
                    "metrics": item.to_dict(),
                }
                for index, item in enumerate(snapshot.excluded, start=1)
            ]
            self.journal.record_universe(
                run_id=uuid.uuid4().hex,
                selected_at_ms=_now_ms(),
                utc_date=day,
                protocol_hash=self.protocol_hash,
                source=payload,
                members=members,
            )
            self.journal.event(_now_ms(), "INFO", "UNIVERSE", f"selected {len(symbols)} symbols", {"symbols": symbols})
            symbol_meta = {item.symbol: item.to_dict() for item in snapshot.selected}

        self.journal.set_meta(
            "current_evaluation_eligible",
            "true" if self.evaluation_eligible else "false",
        )
        missing_meta = [symbol for symbol in symbols if not symbol_meta.get(symbol, {}).get("tick_size")]
        if missing_meta:
            instruments = await asyncio.to_thread(self.client.list_linear_usdt_perpetual_instruments)
            by_symbol = {str(item.get("symbol", "")): item for item in instruments}
            for symbol in missing_meta:
                item = by_symbol.get(symbol, {})
                price_filter = item.get("priceFilter", {}) if isinstance(item, dict) else {}
                lot_filter = item.get("lotSizeFilter", {}) if isinstance(item, dict) else {}
                symbol_meta[symbol] = {
                    **symbol_meta.get(symbol, {}),
                    "tick_size": price_filter.get("tickSize"),
                    "qty_step": lot_filter.get("qtyStep"),
                    "min_order_qty": lot_filter.get("minOrderQty"),
                }
        invalid_meta = [
            symbol
            for symbol in symbols
            if any(
                float(symbol_meta.get(symbol, {}).get(key) or 0) <= 0
                for key in ("tick_size", "qty_step", "min_order_qty")
            )
        ]
        if invalid_meta:
            raise RuntimeError(f"missing positive public instrument metadata: {invalid_meta}")
        self.states = {
            symbol: SymbolState(
                symbol,
                tick_size=float(symbol_meta[symbol]["tick_size"]),
                qty_step=float(symbol_meta[symbol]["qty_step"]),
                min_order_qty=float(symbol_meta[symbol]["min_order_qty"]),
            )
            for symbol in symbols
        }
        for state in self.states.values():
            state.levels = [
                self._level_from_storage(row)
                for row in self.journal.load_levels(symbol=state.symbol, version=DIGASH_LEVEL_VERSION)
            ]
        strategy_params = self.protocol_data["parameters"]["strategy"]
        shadow_params = self.protocol_data["parameters"]["shadow"]
        execution = self.protocol_data["execution_policy"]
        quality = self.protocol_data["parameters"]["data_quality"]
        self.strategies = {}
        self.shadow_retests = {}
        self.shadow_orderflow = {}
        for symbol, state in self.states.items():
            evaluator = StrategyV2Evaluator(
                **{**strategy_params, "tick_size": state.tick_size},
                entry_latency_ms=int(execution["entry_latency_ms"]),
                entry_ttl_ms=int(execution["entry_ttl_ms"]),
                confirmation_timeout_ms=int(execution["confirmation_timeout_ms"]),
                cooldown_ms=int(execution["cooldown_ms"]),
                max_book_age_ms=int(quality["max_book_age_ms"]),
                max_spread_bp=float(self.protocol_data["parameters"]["universe"]["max_spread_bps"]),
                min_depth_notional_top5=float(self.protocol_data["parameters"]["universe"]["min_depth_notional_top5"]),
            )
            evaluator.restore_attempts(self.restored_setup_ids)
            self.strategies[symbol] = evaluator
            self.shadow_retests[symbol] = RetestReclaimShadowEvaluator(
                tick_size=state.tick_size,
                fast_timeframe=self.level_break_fast_timeframe,
                fast_confirming_closes=self.level_break_fast_closes,
                source_confirming_closes=self.level_break_source_closes,
                break_buffer_ticks=self.level_break_buffer_ticks,
                retest_tolerance_ticks=int(shadow_params["retest_tolerance_ticks"]),
                reclaim_buffer_ticks=int(shadow_params["reclaim_buffer_ticks"]),
                max_setup_age_ms=int(shadow_params["max_retest_age_ms"]),
            )
            self.shadow_orderflow[symbol] = ShadowOrderflowEvaluator(
                tick_size=state.tick_size,
                min_wall_age_ms=int(shadow_params["min_wall_age_ms"]),
                wall_failure_remaining_ratio=float(shadow_params["wall_failure_remaining_ratio"]),
                min_cascade_body_to_range=float(strategy_params["cascade_min_body_to_range"]),
                min_cascade_delta_ratio=float(strategy_params["cascade_min_delta_ratio"]),
                stop_buffer_bp=float(strategy_params["stop_buffer_bp"]),
            )
        tick_sizes = {symbol: str(state.tick_size) for symbol, state in self.states.items()}
        wall_thresholds = {
            symbol: str(self.protocol_data["parameters"]["universe"]["min_depth_notional_top5"]) for symbol in symbols
        }
        self.footprint = Footprint(tick_sizes, warmup_ms=self.warmup_ms)
        self.dom = DomTracker(wall_thresholds, warmup_ms=self.warmup_ms)
        await asyncio.gather(*(self._bootstrap(state) for state in self.states.values()))
        stop = asyncio.Event()
        rollover = asyncio.create_task(self._stop_at_deadline(stop, session_deadline))
        heartbeat = asyncio.create_task(self._heartbeat(stop))
        try:
            await run_public_stream(symbols, self.handle_message, stop=stop)
        finally:
            stop.set()
            rollover.cancel()
            heartbeat.cancel()
            await asyncio.gather(rollover, heartbeat, return_exceptions=True)

    async def _bootstrap(self, state: SymbolState) -> None:
        now_ms = _now_ms()
        for name, interval in BYBIT_INTERVALS.items():
            # Bybit's kline endpoint may include the currently forming row when
            # queried at a bucket boundary.  Bound the response strictly before
            # the current bucket so the requested page is completed-only.
            interval_ms = TIMEFRAMES[name]
            completed_end_ms = now_ms - (now_ms % interval_ms) - 1
            payload = await asyncio.to_thread(
                self.client.get_kline,
                symbol=state.symbol,
                interval=interval,
                end=completed_end_ms,
                limit=HISTORY_LIMIT,
            )
            rows = payload.get("result", {}).get("list", [])
            parsed = _parse_klines(state.symbol, name, rows, now_ms)
            state.bars[name].extend(parsed)
            state.bars[name] = state.bars[name][-BAR_RETENTION:]
            state.history_diagnostics[name] = _history_diagnostics(name, state.bars[name], HISTORY_LIMIT)
            self.journal.record_bars(name, parsed, "rest_kline")
        for builder in state.builders.values():
            builder.quarantine_first_bucket()
        self._refresh_digash_levels(state, now_ms)
        self._refresh_level_lifecycle(state, now_ms)
        self.journal.event(_now_ms(), "INFO", "BOOTSTRAP", f"loaded causal bars for {state.symbol}")

    def _refresh_digash_levels(self, state: SymbolState, now_ms: int) -> None:
        """Rebuild the canonical level catalog from completed Bybit bars.

        ``build_reference_levels`` is intentionally pure and causal, so a
        restart and a live refresh produce the same IDs.  We merge its output
        into the durable journal rather than replacing state: this preserves
        absorbing break timestamps when a later 1000-bar window no longer
        contains the historical pivot.
        """
        # Freeze existing cluster breaks before membership can change. Their
        # constituent IDs remain tombstones for subsequent detector runs.
        self._refresh_level_lifecycle(state, now_ms)
        config = GeometryConfig(history_limit=HISTORY_LIMIT, level_version=DIGASH_LEVEL_VERSION)
        detected: list[Level] = []
        for timeframe in DIGASH_TIMEFRAMES:
            result = build_reference_levels(
                state.bars.get(timeframe, ()),
                timeframe=timeframe,
                now_ms=now_ms,
                config=config,
                persisted_levels=[level for level in state.levels if level.timeframe == timeframe],
                lifecycle_bars=state.bars,
                tick_size=state.tick_size,
                buffer_ticks=self.level_break_buffer_ticks,
                fast_timeframe=self.level_break_fast_timeframe,
                fast_confirming_closes=self.level_break_fast_closes,
                source_confirming_closes=self.level_break_source_closes,
            )
            detected.extend(result.levels)
            state.history_diagnostics[timeframe] = {
                **state.history_diagnostics.get(timeframe, {}),
                "digash_coverage": asdict(result.coverage),
            }
        # Replace only detector-owned active catalog entries; legacy 15m/4h
        # records remain available in storage as control diagnostics but can
        # no longer become canonical runtime levels.
        retained = [
            level
            for level in state.levels
            if level.level_version == DIGASH_LEVEL_VERSION and level.broken_at_ms is not None
        ]
        state.levels = retained
        self._merge_detected_levels(state, detected, now_ms)

    async def handle_message(self, message: dict, received_at_ms: int) -> None:
        if message.get("op") == "connection":
            state = str(message.get("state", "unknown"))
            self.journal.set_meta("stream_state", state)
            if state == "disconnected":
                self.stream_connected = False
                for symbol_state in self.states.values():
                    symbol_state.book.invalidate()
                    symbol_state.snapshot_received_at_ms = None
                    for builder in symbol_state.builders.values():
                        builder.quarantine_first_bucket()
                    symbol_state.last_blocker = "stream_disconnected"
                    symbol_state.orderflow_ready = False
                    symbol_state.density_walls.clear()
                    symbol_state.shadow_active = False
                self.executor.cancel_pending("market_stream_disconnected", received_at_ms)
                self._mark_signal_paths_unknown(received_at_ms, "market_stream_disconnected")
                detail = {"error": str(message.get("error", "")), "open_position": bool(self.executor.position)}
                self.journal.event(
                    received_at_ms, "WARN", "STREAM_DISCONNECTED", "public market stream disconnected", detail
                )
            else:
                self.stream_connected = True
                self.connection_id = uuid.uuid4().hex
                self.journal.event(received_at_ms, "INFO", "STREAM_CONNECTED", "public market stream connected")
            return
        topic = str(message.get("topic", ""))
        if topic.startswith("orderbook.50."):
            symbol = topic.rsplit(".", 1)[-1]
            state = self.states.get(symbol)
            if state is None:
                return
            try:
                state.book.apply(message, received_at_ms)
                if message.get("type") == "snapshot" or message.get("data", {}).get("u") == 1:
                    state.snapshot_received_at_ms = received_at_ms
                    state.density_walls.clear()
                    state.shadow_active = False
                wrapped = {"received_at_ms": received_at_ms, "connection_id": self.connection_id, "message": message}
                completed_footprints: list[dict[str, Any]] = []
                if self.footprint is not None:
                    completed_footprints = self.footprint.process(wrapped)
                if self.dom is not None:
                    dom_events = self.dom.process(wrapped)
                    self._update_density_walls(state, dom_events)
                    state.last_dom_events = [*state.last_dom_events, *dom_events][-20:]
                self.executor.on_quote(symbol, self._quote(state, received_at_ms))
                for footprint in completed_footprints:
                    self._complete_footprint(footprint, received_at_ms)
            except (TypeError, ValueError) as exc:
                self.executor.cancel_pending(str(exc), received_at_ms)
                self.journal.event(received_at_ms, "WARN", "BOOK", str(exc), {"symbol": symbol})
                raise
            return
        if topic.startswith("publicTrade."):
            wrapped = {"received_at_ms": received_at_ms, "connection_id": self.connection_id, "message": message}
            completed_footprints = self.footprint.process(wrapped) if self.footprint is not None else []
            if self.dom is not None:
                self.dom.process(wrapped)
            for row in message.get("data", []):
                await self._trade(row, received_at_ms)
                symbol = str(row.get("s", ""))
                self._observe_signal_paths(symbol, received_at_ms, float(row.get("p", 0)))
                execution = self.executor.on_trade(
                    symbol,
                    received_at_ms,
                    str(row.get("S", "")),
                    float(row.get("p", 0)),
                    float(row.get("v", 0)),
                )
                if execution and execution.get("event") in {"OPEN", "PARTIAL"}:
                    self.journal.event(received_at_ms, "INFO", "PAPER_FILL", str(execution["event"]), execution)
            for footprint in completed_footprints:
                self._complete_footprint(footprint, received_at_ms)

    async def _trade(self, row: dict[str, Any], received_at_ms: int) -> None:
        symbol = str(row.get("s", ""))
        state = self.states.get(symbol)
        if state is None:
            return
        try:
            trade = Trade(symbol, received_at_ms, str(row["i"]), float(row["p"]), float(row["v"]), str(row["S"]))
            completed_any = False
            for name in ("1d", "4h", "1h", "30m", "15m", "5m", "1m"):
                for completed in state.builders[name].add(trade):
                    if state.bars[name] and completed.opened_at_ms <= state.bars[name][-1].opened_at_ms:
                        continue
                    state.bars[name].append(completed)
                    completed_any = True
                    self.journal.record_bar(name, completed, "public_trade")
                    state.bars[name] = state.bars[name][-BAR_RETENTION:]
                    state.history_diagnostics[name] = _history_diagnostics(name, state.bars[name], HISTORY_LIMIT)
            if completed_any:
                # Recompute only from completed bars; forming bars never enter
                # the geometry detector or strategy state.
                self._refresh_digash_levels(state, received_at_ms)
                self._refresh_level_lifecycle(state, received_at_ms)
        except (KeyError, TypeError, ValueError) as exc:
            self.journal.event(received_at_ms, "WARN", "TRADE", str(exc), {"symbol": symbol})

    def _quote(self, state: SymbolState, received_at_ms: int) -> Quote:
        return Quote(
            received_at_ms,
            state.book.best_bid,
            state.book.best_ask,
            state.book.bids[state.book.best_bid],
            state.book.asks[state.book.best_ask],
            dict(state.book.bids),
            dict(state.book.asks),
        )

    def _complete_footprint(self, footprint: dict[str, Any], evaluated_at_ms: int | None = None) -> None:
        symbol = str(footprint["symbol"])
        state = self.states.get(symbol)
        if state is None:
            return
        bar = Bar(
            symbol=symbol,
            timeframe_ms=15_000,
            opened_at_ms=int(footprint["bucket_start_ms"]),
            closed_at_ms=int(footprint["bucket_end_ms"]),
            open=float(footprint["open"]),
            high=float(footprint["high"]),
            low=float(footprint["low"]),
            close=float(footprint["close"]),
            volume=float(footprint["volume"]),
            delta_notional=float(footprint["delta_notional"]),
            trades=int(footprint["trades"]),
        )
        if state.bars["15s"] and bar.opened_at_ms <= state.bars["15s"][-1].opened_at_ms:
            return
        state.bars["15s"].append(bar)
        state.bars["15s"] = state.bars["15s"][-BAR_RETENTION:]
        state.history_diagnostics["15s"] = _history_diagnostics("15s", state.bars["15s"], HISTORY_LIMIT)
        self.journal.record_bar("15s", bar, "public_trade_footprint")
        state.orderflow_ready = not bool(footprint.get("incomplete") or footprint.get("partial"))
        if not state.orderflow_ready:
            state.last_orderflow = {
                "delta_15s": bar.delta_notional,
                "footprint_stack": 0,
                "updated_at": _stamp(bar.closed_at_ms),
                "status": "incomplete",
            }
            return
        self._evaluate_v2(state, footprint, evaluated_at_ms or bar.closed_at_ms)

    def _readiness(self, state: SymbolState, now_ms: int) -> dict[str, Any]:
        warmup_remaining = (
            self.warmup_ms
            if state.snapshot_received_at_ms is None
            else max(0, state.snapshot_received_at_ms + self.warmup_ms - now_ms)
        )
        book_ready = state.book.healthy(
            now_ms, self.max_book_age_ms, float(self.protocol_data["parameters"]["universe"]["max_spread_bps"])
        )
        blocker = None
        if not self.evaluation_eligible:
            blocker = "partial_day_observation_only"
        elif not self.stream_connected:
            blocker = "stream_disconnected"
        elif not state.book.ready:
            blocker = "book_snapshot_required"
        elif warmup_remaining > 0:
            blocker = "post_snapshot_warmup"
        elif not book_ready:
            blocker = "book_stale_or_spread"
        elif not state.orderflow_ready:
            blocker = "orderflow_incomplete"
        return {
            "eligible": self.evaluation_eligible,
            "stream_ready": self.stream_connected and state.book.ready,
            "gap_free": state.book.ready and state.snapshot_received_at_ms is not None and state.orderflow_ready,
            "warmup_remaining_s": math.ceil(warmup_remaining / 1000),
            "book_ready": book_ready,
            "blocker": blocker,
            "ready": blocker is None,
        }

    def _evaluate_v2(self, state: SymbolState, footprint: dict[str, Any], now_ms: int) -> None:
        prior = state.bars["15s"][-21:-1]
        median_delta = median(abs(bar.delta_notional) for bar in prior) if prior else 0.0
        ranges = [((bar.high / bar.low) - 1) * 10_000 for bar in prior if bar.low > 0]
        median_range = median(ranges) if ranges else 0.0
        bid_notional = sum(price * size for price, size in sorted(state.book.bids.items(), reverse=True)[:5])
        ask_notional = sum(price * size for price, size in sorted(state.book.asks.items())[:5])
        current = state.bars["15s"][-1]
        orderflow = OrderflowFrame(
            symbol=state.symbol,
            received_at_ms=now_ms,
            best_bid=state.book.best_bid,
            best_bid_size=state.book.bids[state.book.best_bid],
            best_ask=state.book.best_ask,
            best_ask_size=state.book.asks[state.book.best_ask],
            delta_notional=current.delta_notional,
            median_abs_delta_20=median_delta,
            range_bp=(current.high / current.low - 1) * 10_000,
            median_range_bp_20=median_range,
            top5_bid_notional=bid_notional,
            top5_ask_notional=ask_notional,
            atr_1m=self._atr(state.bars["1m"]),
            book_age_ms=max(0, now_ms - int(state.book.received_at_ms or 0)),
            spread_bp=(state.book.best_ask / state.book.best_bid - 1) * 10_000,
        )
        state.last_orderflow = {
            "delta_15s": current.delta_notional,
            "microprice_bias_bp": orderflow.microprice_mid_bp,
            "top5_imbalance": orderflow.book_imbalance,
            "dom_persistence": sum(event.get("type") == "wall_persistent" for event in state.last_dom_events),
            "dom_refill": sum(bool(event.get("refill_candidate")) for event in state.last_dom_events),
            "footprint_stack": len(footprint.get("stacks", [])),
            "updated_at": _stamp(now_ms),
        }
        readiness = self._readiness(state, now_ms)
        blocker = readiness["blocker"]
        if blocker:
            if blocker != state.last_blocker:
                self.journal.event(now_ms, "INFO", "SIGNAL_BLOCK", blocker, {"symbol": state.symbol})
                state.last_blocker = blocker
            return
        state.last_blocker = None
        levels = list(state.levels)
        decisions = self.strategies[state.symbol].evaluate(
            symbol=state.symbol,
            now_ms=now_ms,
            bars=state.bars,
            levels=levels,
            orderflow=orderflow,
        )
        price = state.bars["15s"][-1].close
        for decision in decisions:
            if decision.signal is not None or decision.status == "MISSED" or decision.setup_id is not None:
                decision_key = f"{decision.setup_id}:{decision.status}:{decision.reason}"
                if decision_key in state.recorded_decisions:
                    continue
                state.recorded_decisions.add(decision_key)
                self._record_decision(state, decision, price, now_ms)
        self._evaluate_shadow(state, levels, now_ms)

    @staticmethod
    def _update_density_walls(state: SymbolState, events: list[dict[str, Any]]) -> None:
        for event in events:
            side = str(event.get("side", ""))
            try:
                price = float(event["price"])
                quantity = float(event["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (side, price)
            kind = str(event.get("type", ""))
            if kind == "wall_persistent":
                if key not in state.density_walls and any(existing_side == side for existing_side, _ in state.density_walls):
                    continue
                observed_at = int(event.get("first_candidate_at_ms", event.get("received_ms", 0)))
                state.density_walls[key] = {
                    "side": side,
                    "price": price,
                    "observed_at_ms": observed_at,
                    "initial_size": quantity,
                    "current_remaining": quantity,
                    "source_event_id": hashlib.sha256(
                        f"{state.symbol}|{side}|{price}|{observed_at}".encode()
                    ).hexdigest()[:24],
                    "evidence_quality": "observed",
                }
            elif key in state.density_walls and kind in {"depth_reduced", "depth_added", "level_removed"}:
                state.density_walls[key]["current_remaining"] = quantity
                if kind == "level_removed":
                    state.density_walls[key]["evidence_quality"] = "ambiguous"

    def _evaluate_shadow(self, state: SymbolState, levels: list[Level], now_ms: int) -> None:
        shadow_evaluation_eligible = self.shadow_started_at_ms <= (now_ms // 86_400_000) * 86_400_000 + 300_000
        if not state.shadow_active:
            # Pre-ready walls are observation-only and cannot seed a forward setup.
            state.density_walls.clear()
            state.shadow_active = True
        retest_evaluator = self.shadow_retests.get(state.symbol)
        if retest_evaluator is not None:
            for setup in retest_evaluator.evaluate(
                symbol=state.symbol,
                now_ms=now_ms,
                bars=state.bars,
                levels=state.levels,
            ):
                if (
                    setup.broken_at_ms is None
                    or setup.broken_at_ms < self.shadow_started_at_ms
                    or setup.status is ShadowSetupStatus.REJECTED
                ):
                    continue
                occurred_at = setup.reclaim_at_ms or setup.invalidated_at_ms or setup.retest_at_ms or setup.broken_at_ms
                features = {
                    **dict(setup.features),
                    "level_id": setup.level_id,
                    "level_timeframe": setup.level_timeframe,
                    "phase": setup.phase.value,
                    "broken_at_ms": setup.broken_at_ms,
                    "evaluation_eligible": "true" if shadow_evaluation_eligible else "false",
                }
                self._record_shadow(
                    {
                        "diagnostic_id": setup.setup_id,
                        "setup_id": f"{state.symbol}:retest_reclaim_v1:{setup.level_id}:{setup.broken_at_ms}",
                        "occurred_at_ms": occurred_at,
                        "symbol": state.symbol,
                        "lane": setup.lane,
                        "side": setup.side.value,
                        "status": setup.status.value,
                        "reason": setup.reason,
                        "reference_price": setup.level_price,
                        "stop_price": setup.stop_price,
                        "target_price": setup.target_price,
                        "protocol_hash": self.protocol_hash,
                        "features": features,
                    }
                )

        orderflow_evaluator = self.shadow_orderflow.get(state.symbol)
        if orderflow_evaluator is None:
            return
        cascade = orderflow_evaluator.cascade_terminal_exit(
            symbol=state.symbol,
            now_ms=now_ms,
            bars=state.bars,
            levels=levels,
        )
        if cascade.status is ShadowStatus.OBSERVED or cascade.reason in {
            "blocked_no_causal_active_level",
            "waiting_for_close_through",
        }:
            record = cascade.to_record(self.protocol_hash)
            record["features"] = {
                **dict(record["features"]),
                "evaluation_eligible": "true" if shadow_evaluation_eligible else "false",
            }
            self._record_shadow(record)

        expired_walls: list[tuple[str, float]] = []
        for key, wall in state.density_walls.items():
            observed_at = int(wall["observed_at_ms"])
            diagnostic = orderflow_evaluator.density_bounce_v1(
                symbol=state.symbol,
                now_ms=now_ms,
                bars=state.bars,
                wall=DensityWallEvidence(
                    symbol=state.symbol,
                    side=PaperSide.LONG if wall["side"] == "bid" else PaperSide.SHORT,
                    price=float(wall["price"]),
                    observed_at_ms=observed_at,
                    wall_age_ms=max(0, now_ms - observed_at),
                    initial_size=float(wall["initial_size"]),
                    current_remaining=float(wall["current_remaining"]),
                    source_event_id=str(wall["source_event_id"]),
                    evidence_quality=str(wall["evidence_quality"]),
                ),
                levels=levels,
            )
            completed_touch = any(
                bar.closed_at_ms <= now_ms
                and bar.closed_at_ms >= observed_at
                and bar.low <= float(wall["price"]) <= bar.high
                for bar in (state.bars["15s"] or state.bars["1m"])
            )
            persist_density = diagnostic.status is ShadowStatus.OBSERVED or (
                completed_touch
                and diagnostic.reason
                in {
                    "blocked_ambiguous_wall_removal",
                    "wall_eroded_to_half",
                    "first_approach_already_consumed",
                    "wall_broken",
                    "touch_without_confirmation",
                }
            )
            if persist_density:
                record = diagnostic.to_record(self.protocol_hash)
                record["features"] = {
                    **dict(record["features"]),
                    "evaluation_eligible": "true" if shadow_evaluation_eligible else "false",
                }
                self._record_shadow(record)
            if now_ms - observed_at > 30 * 60_000 or wall["evidence_quality"] == "ambiguous":
                expired_walls.append(key)
        for key in expired_walls:
            state.density_walls.pop(key, None)

    def _record_shadow(self, record: dict[str, Any]) -> bool:
        row = dict(record)
        raw_id = f"{self.protocol_hash}|{record['diagnostic_id']}"
        row["diagnostic_id"] = hashlib.sha256(raw_id.encode()).hexdigest()[:24]
        return self.journal.record_shadow_diagnostic(row)

    @staticmethod
    def _atr(bars: list[Bar], period: int = 14) -> float:
        if len(bars) < period + 1:
            return 0.0
        sample = bars[-(period + 1) :]
        values = [
            max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
            for previous, bar in pairwise(sample)
        ]
        return sum(values) / len(values)

    def _refresh_level_lifecycle(self, state: SymbolState, now_ms: int) -> None:
        before = {level.level_id: level for level in state.levels}
        refreshed = apply_level_breaks(
            state.levels,
            state.bars,
            now_ms,
            tick_size=state.tick_size,
            buffer_ticks=self.level_break_buffer_ticks,
            fast_timeframe=self.level_break_fast_timeframe,
            fast_confirming_closes=self.level_break_fast_closes,
            source_confirming_closes=self.level_break_source_closes,
        )
        durable: list[Level] = []
        for level in refreshed:
            prior = before.get(level.level_id)
            if level.broken_at_ms is not None and (prior is None or prior.broken_at_ms != level.broken_at_ms):
                if self.journal.level_row(level.level_id) is None:
                    self.journal.upsert_level(self._level_storage_mapping(prior or level, level.confirmed_at_ms))
                stored = self.journal.mark_level_broken(
                    level.level_id,
                    level.broken_at_ms,
                    "confirmed_close_break",
                    updated_at_ms=now_ms,
                )
                level = self._level_from_storage(stored)
            durable.append(level)
        state.levels = durable

    def _merge_detected_levels(self, state: SymbolState, detected: list[Level], now_ms: int) -> None:
        """Merge detector output with durable, absorbing lifecycle state."""
        by_id = {level.level_id: level for level in state.levels}
        pending = []
        for level in detected:
            prior = by_id.get(level.level_id)
            if prior is not None:
                level = replace(
                    level,
                    revision=max(level.revision, prior.revision),
                    first_seen_at_ms=prior.first_seen_at_ms,
                )
            pending.append(self._level_storage_mapping(level, now_ms))
        for stored in self.journal.upsert_levels(pending):
            by_id[str(stored["level_id"])] = self._level_from_storage(stored)
        state.levels = sorted(
            by_id.values(),
            key=lambda item: (item.timeframe, item.confirmed_at_ms, item.level_id),
        )

    def _level_storage_mapping(self, level: Level, now_ms: int) -> dict[str, Any]:
        return {
            **asdict(level),
            "side": level.side.value,
            "revision": level.revision,
            "version": level.level_version or self.level_version,
            "zone_low": level.price if level.zone_low is None else level.zone_low,
            "zone_high": level.price if level.zone_high is None else level.zone_high,
            "first_seen_at_ms": (
                now_ms if level.first_seen_at_ms is None else level.first_seen_at_ms
            ),
            "updated_at_ms": max(
                level.confirmed_at_ms,
                now_ms if level.first_seen_at_ms is None else level.first_seen_at_ms,
                level.broken_at_ms or 0,
            ),
            "provenance": {
                "level_class": level.level_class,
                "detector": "causal_pivot",
                **level.provenance,
            },
        }

    @staticmethod
    def _level_from_storage(row: dict[str, Any]) -> Level:
        return Level(
            level_id=str(row["level_id"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            side=LevelSide(str(row["side"])),
            price=float(row["price"]),
            confirmed_at_ms=int(row["confirmed_at_ms"]),
            touches=int(row["touches"]),
            level_class=str(row["level_class"]),
            origin_at_ms=int(row["origin_at_ms"]),
            broken_at_ms=None if row["broken_at_ms"] is None else int(row["broken_at_ms"]),
            zone_low=float(row["zone_low"]),
            zone_high=float(row["zone_high"]),
            revision=int(row["revision"]),
            level_version=str(row["version"]),
            first_seen_at_ms=int(row["first_seen_at_ms"]),
            invalidation_reason=(
                None if row["invalidation_reason"] is None else str(row["invalidation_reason"])
            ),
            provenance=dict(row.get("provenance", {})),
        )

    def _record_decision(self, state: SymbolState, decision: StrategyDecisionV2, price: float, now_ms: int) -> None:
        target = decision.target_level
        signal = decision.signal
        signal_id = signal.signal_id if signal else uuid.uuid4().hex
        side = signal.side.value if signal else (decision.side.value if decision.side else "LONG")
        features = dict(decision.features)
        features["session_id"] = self.session_id
        features["versions"] = self.version_info
        features["data_coverage"] = self._metric_snapshot(state, now_ms)
        features["liquidity_observation"] = self._liquidity_snapshot(state, now_ms)
        if target:
            features["level_id"] = target.level_id
        if decision.setup_id:
            features["setup_id"] = decision.setup_id
        row = {
            "signal_id": signal_id,
            "occurred_at_ms": now_ms,
            "symbol": state.symbol,
            "side": side,
            "lane": decision.lane,
            "target_timeframe": target.timeframe if target else "none",
            "level_class": target.level_class if target else "none",
            "trigger_price": price,
            "stop_price": signal.stop_price if signal else (decision.stop_price or price),
            "target_price": signal.target_price if signal else (decision.target_price or price),
            "status": decision.status,
            "reason": decision.reason,
            "protocol_hash": self.protocol_hash,
            "features": features,
        }
        self.journal.record_signal(row)
        if signal:
            quote = self._quote(state, now_ms)
            bracket_reason = self._signal_bracket_reason(
                signal,
                path_entry=price,
                executable_entry=quote.bid if signal.side is PaperSide.LONG else quote.ask,
            )
            if bracket_reason is not None:
                # A malformed strategy signal is a rejected record, not a
                # stream error.  In particular, do not start a path whose
                # geometry cannot be evaluated and do not call the executor.
                self.journal.update_signal_status(signal.signal_id, "REJECTED", bracket_reason)
                self.journal.event(
                    now_ms,
                    "WARN",
                    "SIGNAL_REJECTED",
                    bracket_reason,
                    {"symbol": state.symbol, "signal_id": signal.signal_id, "lane": signal.lane},
                )
                return
            self._start_signal_path(state, signal, price, now_ms)
            blocker = self.executor.submission_blocker(signal)
            if blocker:
                self.journal.update_signal_status(signal.signal_id, "MISSED", blocker)
            else:
                submitted = self.executor.submit(
                    signal,
                    quote,
                    qty_step=state.qty_step,
                    min_order_qty=state.min_order_qty,
                )
                if not submitted:
                    self.journal.update_signal_status(signal.signal_id, "MISSED", "post_only_submission_rejected")

    @staticmethod
    def _signal_bracket_reason(
        signal: PaperSignal,
        *,
        path_entry: float,
        executable_entry: float,
    ) -> str | None:
        prices = (path_entry, executable_entry, signal.stop_price, signal.target_price)
        if not all(math.isfinite(value) and value > 0 for value in prices):
            return "invalid_signal_bracket"
        if signal.side is PaperSide.LONG:
            valid = signal.stop_price < path_entry < signal.target_price and signal.stop_price < executable_entry < signal.target_price
        else:
            valid = signal.target_price < path_entry < signal.stop_price and signal.target_price < executable_entry < signal.stop_price
        return None if valid else "invalid_signal_bracket"

    def _start_signal_path(
        self,
        state: SymbolState,
        signal: PaperSignal,
        entry_reference: float,
        now_ms: int,
    ) -> None:
        tracker = SignalPathTracker(
            signal.signal_id,
            side="buy" if signal.side is PaperSide.LONG else "sell",
            entry_price=entry_reference,
            target_price=signal.target_price,
            stop_price=signal.stop_price,
            start_at_ms=now_ms,
        )
        self.signal_paths[signal.signal_id] = (state.symbol, IncrementalSignalPath(tracker))
        self.journal.record_signal_path_event(
            {
                "event_id": f"{signal.signal_id}:START",
                "signal_id": signal.signal_id,
                "occurred_at_ms": now_ms,
                "event_type": "START",
                "reference_price": entry_reference,
                "tp_touched": "UNKNOWN",
                "sl_touched": "UNKNOWN",
                "coverage": 1.0,
                "status": "ACTIVE",
                "reason": "admitted_signal_path",
                "features": {
                    "entry_reference": entry_reference,
                    "target_price": signal.target_price,
                    "stop_price": signal.stop_price,
                    "horizon_ms": self.signal_path_horizon_ms,
                },
                "provenance": {
                    "session_id": self.session_id,
                    "protocol_hash": self.protocol_hash,
                    "versions": self.version_info,
                    "lane": signal.lane,
                    "symbol": state.symbol,
                    "data_source": "public_trade_ticks",
                    "not_a_fill": True,
                },
            }
        )

    def _observe_signal_paths(self, symbol: str, occurred_at_ms: int, price: float) -> None:
        completed: list[str] = []
        for signal_id, (path_symbol, live) in self.signal_paths.items():
            if path_symbol != symbol:
                continue
            deadline = live.tracker.start_at_ms + self.signal_path_horizon_ms
            result = (
                live.timeout(deadline)
                if occurred_at_ms >= deadline
                else live.observe_trade(TradeTick(occurred_at_ms, price))
            )
            if result.status in {
                PathStatus.TP_TOUCHED,
                PathStatus.SL_TOUCHED,
                PathStatus.TIMEOUT,
                PathStatus.AMBIGUOUS,
                PathStatus.UNKNOWN,
            }:
                self._record_signal_path_result(result)
                completed.append(signal_id)
        for signal_id in completed:
            self.signal_paths.pop(signal_id, None)

    def _mark_signal_paths_unknown(self, occurred_at_ms: int, reason: str) -> None:
        for signal_id, (_, live) in list(self.signal_paths.items()):
            self._record_signal_path_result(live.mark_unknown(occurred_at_ms, reason))
            self.signal_paths.pop(signal_id, None)

    def _record_signal_path_result(self, result: SignalPathResult) -> None:
        self.journal.record_signal_path_event(
            {
                "event_id": f"{result.signal_id}:{result.status.value}",
                "signal_id": result.signal_id,
                "occurred_at_ms": result.first_touch_at_ms or result.covered_until_ms or result.start_at_ms,
                "event_type": result.status.value,
                "price": result.first_touch_price,
                "reference_price": result.entry_price,
                "tp_touched": "YES" if result.status is PathStatus.TP_TOUCHED else (
                    "NO" if result.status is PathStatus.SL_TOUCHED else result.status.value
                    if result.status in {PathStatus.AMBIGUOUS, PathStatus.UNKNOWN}
                    else "NO"
                ),
                "sl_touched": "YES" if result.status is PathStatus.SL_TOUCHED else (
                    "NO" if result.status is PathStatus.TP_TOUCHED else result.status.value
                    if result.status in {PathStatus.AMBIGUOUS, PathStatus.UNKNOWN}
                    else "NO"
                ),
                "mfe_bp": result.mfe_bps,
                "mae_bp": result.mae_bps,
                "coverage": 1.0 if result.coverage_complete else 0.0,
                "status": result.status.value,
                "reason": result.coverage_reason,
                "features": result.to_dict(),
                "provenance": {"not_a_fill": True, "source": "signal_path_tracker_v1"},
            }
        )

    def _reconcile_orphaned_signal_paths(self) -> int:
        with self.journal.connect() as db:
            active = db.execute(
                """SELECT current.signal_id
                   FROM signal_path_events AS current
                   WHERE current.status='ACTIVE'
                     AND NOT EXISTS (
                       SELECT 1 FROM signal_path_events AS later
                       WHERE later.signal_id=current.signal_id
                         AND (later.occurred_at_ms>current.occurred_at_ms OR (
                           later.occurred_at_ms=current.occurred_at_ms AND later.event_id>current.event_id
                         ))
                     )"""
            ).fetchall()
        now_ms = _now_ms()
        for event in active:
            self.journal.record_signal_path_event(
                {
                    "event_id": f"{event['signal_id']}:RESTART_UNKNOWN:{now_ms}",
                    "signal_id": event["signal_id"],
                    "occurred_at_ms": now_ms,
                    "event_type": "UNKNOWN",
                    "tp_touched": "UNKNOWN",
                    "sl_touched": "UNKNOWN",
                    "coverage": 0.0,
                    "status": "UNKNOWN",
                    "reason": "restart_without_tick_buffer",
                    "provenance": {"not_a_fill": True},
                }
            )
        return len(active)

    def _mark_closed_signal(self, position_id: str, reason: str) -> None:
        with self.journal.connect() as db:
            row = db.execute("SELECT signal_id FROM positions WHERE position_id=?", (position_id,)).fetchone()
        if row:
            self.journal.update_signal_status(str(row["signal_id"]), "CLOSED", reason)

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            ready = sum(state.book.ready for state in self.states.values())
            self.journal.event(
                _now_ms(), "INFO", "HEARTBEAT", f"paper service healthy; books {ready}/{len(self.states)}"
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _stop_at_deadline(self, stop: asyncio.Event, deadline: datetime) -> None:
        now = datetime.now(UTC)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, (deadline - now).total_seconds()))
        except TimeoutError:
            stop.set()

    def market_detail(self, symbol: str, timeframe: str) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        state = self.states.get(symbol)
        if state is None:
            raise ValueError("symbol is not in the daily universe")
        now_ms = _now_ms()
        forming = state.builders[timeframe].current()
        if forming is not None and forming.closed_at_ms <= now_ms:
            forming = None
        completed = state.bars[timeframe]
        state.history_diagnostics[timeframe] = _history_diagnostics(timeframe, completed, HISTORY_LIMIT)
        if forming is not None and completed and forming.opened_at_ms <= completed[-1].opened_at_ms:
            forming = None
        bars = completed[-(179 if forming is not None else 180) :]
        bar_payload = [{**asdict(bar), "is_forming": False} for bar in bars]
        if forming is not None:
            bar_payload.append({**asdict(forming), "is_forming": True})
        last_price = forming.close if forming is not None else bars[-1].close if bars else None
        levels = current_display_levels(state.levels, symbol, last_price, now_ms) if last_price is not None else []
        quote: dict[str, float] | None = None
        if state.book.ready:
            bid, ask = state.book.best_bid, state.book.best_ask
            quote = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        positions = []
        row = self.journal.open_position_row()
        if row:
            positions.append(row)
        open_orders: list[dict[str, Any]] = []
        if self.executor.pending:
            pending = self.executor.pending
            open_orders.append(
                {
                    "time": _stamp(pending.created_at_ms),
                    "symbol": pending.signal.symbol,
                    "type": "POST-ONLY ENTRY",
                    "side": pending.signal.side.value,
                    "price": pending.entry_price,
                    "status": pending.status,
                    "filled_qty": pending.filled_qty,
                    "quantity": pending.quantity,
                    "queue_ahead_qty": pending.queue_ahead_qty,
                }
            )
        if row:
            open_orders.extend(
                [
                    {
                        "time": _stamp(int(row["opened_at_ms"])),
                        "symbol": row["symbol"],
                        "type": "STOP LOSS",
                        "side": row["side"],
                        "price": row["stop_price"],
                        "status": "OPEN",
                    },
                    {
                        "time": _stamp(int(row["opened_at_ms"])),
                        "symbol": row["symbol"],
                        "type": "TAKE PROFIT",
                        "side": row["side"],
                        "price": row["target_price"],
                        "status": "OPEN",
                    },
                ]
            )
        history = []
        for item in self.journal.position_history():
            item["closed_at"] = _stamp(int(item["closed_at_ms"]))
            history.append(item)
        shadow_diagnostics = self.journal.shadow_diagnostics(symbol, limit=50, protocol_hash=self.protocol_hash)
        for item in shadow_diagnostics:
            item["occurred_at"] = _stamp(int(item["occurred_at_ms"]))
        signal_paths = self.journal.signal_path_events_for_symbol(symbol, limit=50)
        for item in signal_paths:
            item["occurred_at"] = _stamp(int(item["occurred_at_ms"]))
        metrics = self._metric_snapshot(state, now_ms)
        liquidity = self._liquidity_snapshot(state, now_ms)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": bar_payload,
            "history_diagnostics": state.history_diagnostics,
            "metrics": metrics,
            "liquidity": liquidity,
            "server_time_ms": now_ms,
            "bar_closes_at_ms": (now_ms // TIMEFRAMES[timeframe] + 1) * TIMEFRAMES[timeframe],
            "levels": [asdict(level) for level in levels],
            "level_history": [
                asdict(level)
                for level in sorted(
                    (item for item in state.levels if item.broken_at_ms is not None),
                    key=lambda item: (item.broken_at_ms or 0, item.level_id),
                    reverse=True,
                )[:20]
            ],
            "quote": quote,
            "last_price": last_price,
            "positions": positions,
            "open_orders": open_orders,
            "history": history,
            "shadow_diagnostics": shadow_diagnostics,
            "signal_paths": signal_paths,
            "readiness": self._readiness(state, now_ms),
            "orderflow": state.last_orderflow,
            "versions": self.version_info,
            "session_id": self.session_id,
        }

    def _metric_snapshot(self, state: SymbolState, now_ms: int) -> dict[str, Any]:
        """Return observational Digash metrics without changing trade eligibility."""

        five = state.bars["5m"]
        metrics: dict[str, Any] = {
            "mode": "shadow_observation_only",
            "natr_5m_14_pct": _metric_payload(natr_5m_14(five, now_ms=now_ms)),
            "price_change_24h_pct": _metric_payload(signed_price_change(five, 24 * 60, now_ms=now_ms)),
            "volume_splash_2h": _metric_payload(volume_splash(five, comparison_window=24, now_ms=now_ms)),
            "dollar_volume_24h_pq": _windowed_dollar_volume(five, 288, now_ms),
        }
        btc = self.states.get("BTCUSDT")
        if btc is None:
            metrics["btc_correlation_6h"] = _unavailable_metric("btc_context_not_in_daily_universe", 72)
        else:
            coin_window = five[-73:]
            btc_window = btc.bars["5m"][-73:]
            metrics["btc_correlation_6h"] = _metric_payload(
                btc_correlation(coin_window, btc_window, min_samples=72, now_ms=now_ms)
            )
        required = (
            "natr_5m_14_pct",
            "price_change_24h_pct",
            "volume_splash_2h",
            "dollar_volume_24h_pq",
        )
        unavailable = [name for name in required if not metrics[name]["available"]]
        metrics["readiness"] = {
            "ready": not unavailable,
            "required": list(required),
            "unavailable": unavailable,
        }
        state.metric_snapshot = metrics
        return metrics

    def _liquidity_snapshot(self, state: SymbolState, now_ms: int) -> dict[str, Any]:
        """Observe executable depth without introducing an unverified gate."""

        if not state.book.ready or state.book.received_at_ms is None:
            return {"mode": "shadow_observation_only", "status": "unavailable", "reason": "book_not_ready"}
        book_age_ms = now_ms - state.book.received_at_ms
        if book_age_ms < 0 or book_age_ms > self.max_book_age_ms:
            return {
                "mode": "shadow_observation_only",
                "status": "unavailable",
                "reason": "stale_book",
                "book_age_ms": book_age_ms,
            }
        bids = list(state.book.bids.items())
        asks = list(state.book.asks.items())
        requested: dict[str, float] = {"author_reference_50000_usd": 50_000.0}
        pending = self.executor.pending
        if pending is not None and pending.signal.symbol == state.symbol and pending.quantity > 0:
            requested["current_paper_order_usd"] = pending.quantity * pending.entry_price
        observations = {
            label: {
                "quote_notional": notional,
                "buy": _liquidity_payload(
                    calculate_liquidity_impact(bids, asks, notional, side="buy", reference="mid")
                ),
                "sell": _liquidity_payload(
                    calculate_liquidity_impact(bids, asks, notional, side="sell", reference="mid")
                ),
            }
            for label, notional in requested.items()
        }
        return {
            "mode": "shadow_observation_only",
            "status": "observed",
            "book_age_ms": book_age_ms,
            "book_depth_contract": "orderbook.50",
            "observations": observations,
        }


def _history_diagnostics(timeframe: str, bars: list[Bar], requested_bars: int = HISTORY_LIMIT) -> dict[str, Any]:
    """Describe observed history without fabricating gaps before first listing data."""

    interval = TIMEFRAMES[timeframe]
    ordered = sorted({bar.opened_at_ms: bar for bar in bars}.values(), key=lambda bar: bar.opened_at_ms)
    gaps: list[dict[str, int]] = []
    for previous, current in pairwise(ordered):
        missing = (current.opened_at_ms - previous.opened_at_ms) // interval - 1
        if missing > 0:
            gaps.append(
                {
                    "from_opened_at_ms": previous.opened_at_ms,
                    "to_opened_at_ms": current.opened_at_ms,
                    "missing_bars": missing,
                }
            )
    received = len(ordered)
    interior_missing = sum(item["missing_bars"] for item in gaps)
    short_history = received < requested_bars
    if gaps:
        status = "gapped"
    elif short_history:
        status = "incomplete_history"
    else:
        status = "complete"
    return {
        "timeframe": timeframe,
        "interval_ms": interval,
        "requested_bars": requested_bars,
        "received_bars": received,
        "first_opened_at_ms": ordered[0].opened_at_ms if ordered else None,
        "last_opened_at_ms": ordered[-1].opened_at_ms if ordered else None,
        "interior_gap_count": len(gaps),
        "interior_missing_bars": interior_missing,
        "gaps": gaps,
        "history_complete": status == "complete",
        "coverage_status": status,
        # A short contiguous response is not a data gap; it can be a young
        # listing (or an exchange-side history limit), so leave that explicit.
        "short_contiguous_history": short_history and not gaps,
    }


def _metric_payload(result: MetricResult) -> dict[str, Any]:
    return asdict(result) | {"available": result.available}


def _unavailable_metric(reason: str, expected_samples: int) -> dict[str, Any]:
    return _metric_payload(MetricResult(None, 0, expected_samples, 0.0, reason))


def _windowed_dollar_volume(bars: list[Bar], expected_bars: int, now_ms: int) -> dict[str, Any]:
    completed = [bar for bar in bars if bar.closed_at_ms <= now_ms]
    if len(completed) < expected_bars:
        return _metric_payload(
            MetricResult(
                None,
                len(completed),
                expected_bars,
                len(completed) / expected_bars,
                "insufficient_completed_bars",
            )
        )
    window = completed[-expected_bars:]
    interval = window[0].timeframe_ms
    if any(current.opened_at_ms - previous.opened_at_ms != interval for previous, current in pairwise(window)):
        return _unavailable_metric("missing_bar_in_window", expected_bars)
    return _metric_payload(dollar_volume(window, now_ms=now_ms))


def _liquidity_payload(result: LiquidityImpact) -> dict[str, Any]:
    return asdict(result)


def _parse_klines(symbol: str, timeframe: str, rows: list, now_ms: int) -> list[Bar]:
    milliseconds = TIMEFRAMES[timeframe]
    result: list[Bar] = []
    for row in reversed(rows):
        if not isinstance(row, list) or len(row) < 7:
            continue
        opened = int(row[0])
        if opened + milliseconds > now_ms:
            continue
        result.append(
            Bar(
                symbol,
                milliseconds,
                opened,
                opened + milliseconds,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
                0.0,
                0,
            )
        )
    return result


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _stamp(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def run(database: Path, dashboard_host: str, dashboard_port: int, *, allow_nonloopback_dashboard: bool = False) -> None:
    journal = Journal(database)
    app = PaperApp(journal)
    dashboard, thread = start_dashboard(
        journal,
        dashboard_host,
        dashboard_port,
        allow_nonloopback=allow_nonloopback_dashboard,
        market_provider=app.market_detail,
    )
    try:
        asyncio.run(app.run_forever())
    finally:
        dashboard.shutdown()
        dashboard.server_close()
        thread.join(timeout=2)
