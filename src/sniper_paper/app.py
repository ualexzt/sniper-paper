"""Live public-market orchestration for forward paper evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from .bybit_public import BybitPublicClient
from .market import Bar, BarBuilder, Book, Trade
from .orderflow import DomTracker, Footprint
from .paper import PaperExecutor, Quote
from .paper import Side as PaperSide
from .shadow_orderflow import DensityWallEvidence, ShadowOrderflowEvaluator
from .shadow_setups import RetestReclaimShadowEvaluator, ShadowSetupStatus
from .storage import Journal
from .strategy import (
    CausalLevelEngine,
    Level,
    apply_level_breaks,
    current_display_levels,
    previous_utc_day_levels,
)
from .strategy_v2 import (
    Level as V2Level,
)
from .strategy_v2 import (
    LevelSide as V2LevelSide,
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

TIMEFRAMES = {"15s": 15_000, "1m": 60_000, "5m": 300_000, "15m": 900_000, "4h": 14_400_000}
BYBIT_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "4h": "240"}
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
    builders: dict[str, BarBuilder] = field(init=False)
    level_engines: dict[str, CausalLevelEngine] = field(init=False)
    levels: list[Level] = field(default_factory=list)
    snapshot_received_at_ms: int | None = None
    last_orderflow: dict[str, Any] | None = None
    last_dom_events: list[dict[str, Any]] = field(default_factory=list)
    density_walls: dict[tuple[str, float], dict[str, Any]] = field(default_factory=dict)
    last_blocker: str | None = None
    orderflow_ready: bool = False
    recorded_decisions: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.book = Book(self.symbol)
        self.builders = {name: BarBuilder(self.symbol, milliseconds) for name, milliseconds in TIMEFRAMES.items()}
        self.level_engines = {name: CausalLevelEngine(name) for name in ("15m", "4h")}


class PaperApp:
    def __init__(self, journal: Journal, client: BybitPublicClient | None = None) -> None:
        self.journal = journal
        self.client = client or BybitPublicClient()
        orphaned = self.journal.reconcile_orphaned_triggers()
        self.protocol_data = json.loads(V2_PROTOCOL_PATH.read_text(encoding="utf-8"))
        self.protocol_hash = hashlib.sha256(V2_PROTOCOL_PATH.read_bytes()).hexdigest()
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
        session_deadline = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        existing = self.journal.universe_for_date(day)
        symbol_meta: dict[str, dict[str, Any]] = {}
        if existing:
            symbols = [str(row["symbol"]) for row in existing]
            for row in existing:
                metrics = json.loads(str(row.get("metrics_json", "{}")))
                symbol_meta[str(row["symbol"])] = metrics
            run = self.journal.universe_run_for_date(day)
            self.evaluation_eligible = bool(run and run["source"].get("evaluation_eligible", False))
            self.journal.event(_now_ms(), "INFO", "UNIVERSE_RESTORE", f"restored {len(symbols)} daily symbols")
        else:
            snapshot = await asyncio.to_thread(self.selector.build_snapshot, now)
            symbols = [item.symbol for item in snapshot.selected]
            if not symbols:
                raise RuntimeError("daily selector returned no eligible symbols")
            payload = snapshot.to_dict()
            seconds_after_midnight = (now - datetime.combine(now.date(), datetime.min.time(), UTC)).total_seconds()
            payload["selection_mode"] = "utc_anchor" if seconds_after_midnight <= 300 else "partial_day_bootstrap"
            payload["evaluation_eligible"] = seconds_after_midnight <= 300
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
        for name, interval in BYBIT_INTERVALS.items():
            payload = await asyncio.to_thread(self.client.get_kline, symbol=state.symbol, interval=interval, limit=200)
            rows = payload.get("result", {}).get("list", [])
            parsed = _parse_klines(state.symbol, name, rows, _now_ms())
            state.bars[name].extend(parsed)
            self.journal.record_bars(name, parsed, "rest_kline")
            if name in state.level_engines:
                for bar in parsed:
                    state.levels.extend(state.level_engines[name].add(bar))
        state.levels.extend(previous_utc_day_levels(state.symbol, state.bars["15m"], _now_ms()))
        self._refresh_level_lifecycle(state, _now_ms())
        self.journal.event(_now_ms(), "INFO", "BOOTSTRAP", f"loaded causal bars for {state.symbol}")

    async def handle_message(self, message: dict, received_at_ms: int) -> None:
        if message.get("op") == "connection":
            state = str(message.get("state", "unknown"))
            self.journal.set_meta("stream_state", state)
            if state == "disconnected":
                self.stream_connected = False
                for symbol_state in self.states.values():
                    symbol_state.book.invalidate()
                    symbol_state.snapshot_received_at_ms = None
                    symbol_state.last_blocker = "stream_disconnected"
                    symbol_state.orderflow_ready = False
                    symbol_state.density_walls.clear()
                self.executor.cancel_pending("market_stream_disconnected", received_at_ms)
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
            for name in ("4h", "15m", "5m", "1m"):
                for completed in state.builders[name].add(trade):
                    if state.bars[name] and completed.opened_at_ms <= state.bars[name][-1].opened_at_ms:
                        continue
                    state.bars[name].append(completed)
                    completed_any = True
                    self.journal.record_bar(name, completed, "public_trade")
                    state.bars[name] = state.bars[name][-500:]
                    if name in state.level_engines:
                        state.levels.extend(state.level_engines[name].add(completed))
            if completed_any:
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
        state.bars["15s"] = state.bars["15s"][-500:]
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
        levels = [
            V2Level(
                level.level_id,
                level.symbol,
                level.timeframe,
                V2LevelSide(level.side.value),
                level.price,
                level.confirmed_at_ms,
                level.touches,
                level.level_class,
                level.origin_at_ms,
                level.broken_at_ms,
            )
            for level in state.levels
        ]
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

    def _evaluate_shadow(self, state: SymbolState, levels: list[V2Level], now_ms: int) -> None:
        shadow_evaluation_eligible = self.shadow_started_at_ms <= (now_ms // 86_400_000) * 86_400_000 + 300_000
        retest_evaluator = self.shadow_retests.get(state.symbol)
        if retest_evaluator is not None:
            for setup in retest_evaluator.evaluate(
                symbol=state.symbol,
                now_ms=now_ms,
                bars=state.bars,
                levels=state.levels,
            ):
                if setup.broken_at_ms is None or setup.status is ShadowSetupStatus.REJECTED:
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
                self.journal.record_shadow_diagnostic(
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
        if cascade.reason != "waiting_for_terminal_context":
            record = cascade.to_record(self.protocol_hash)
            record["features"] = {
                **dict(record["features"]),
                "evaluation_eligible": "true" if shadow_evaluation_eligible else "false",
            }
            self.journal.record_shadow_diagnostic(record)

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
            record = diagnostic.to_record(self.protocol_hash)
            record["features"] = {
                **dict(record["features"]),
                "evaluation_eligible": "true" if shadow_evaluation_eligible else "false",
            }
            self.journal.record_shadow_diagnostic(record)
            if now_ms - observed_at > 30 * 60_000 or wall["evidence_quality"] == "ambiguous":
                expired_walls.append(key)
        for key in expired_walls:
            state.density_walls.pop(key, None)

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
        state.levels = apply_level_breaks(
            state.levels,
            state.bars,
            now_ms,
            tick_size=state.tick_size,
            buffer_ticks=self.level_break_buffer_ticks,
            fast_timeframe=self.level_break_fast_timeframe,
            fast_confirming_closes=self.level_break_fast_closes,
            source_confirming_closes=self.level_break_source_closes,
        )

    def _record_decision(self, state: SymbolState, decision: StrategyDecisionV2, price: float, now_ms: int) -> None:
        target = decision.target_level
        signal = decision.signal
        signal_id = signal.signal_id if signal else uuid.uuid4().hex
        side = signal.side.value if signal else (decision.side.value if decision.side else "LONG")
        features = dict(decision.features)
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
            blocker = self.executor.submission_blocker(signal)
            if blocker:
                self.journal.update_signal_status(signal.signal_id, "MISSED", blocker)
            else:
                submitted = self.executor.submit(
                    signal,
                    self._quote(state, now_ms),
                    qty_step=state.qty_step,
                    min_order_qty=state.min_order_qty,
                )
                if not submitted:
                    self.journal.update_signal_status(signal.signal_id, "MISSED", "post_only_submission_rejected")

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
        shadow_diagnostics = self.journal.shadow_diagnostics(symbol, limit=50)
        for item in shadow_diagnostics:
            item["occurred_at"] = _stamp(int(item["occurred_at_ms"]))
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": bar_payload,
            "server_time_ms": now_ms,
            "bar_closes_at_ms": (now_ms // TIMEFRAMES[timeframe] + 1) * TIMEFRAMES[timeframe],
            "levels": [asdict(level) for level in levels],
            "quote": quote,
            "last_price": last_price,
            "positions": positions,
            "open_orders": open_orders,
            "history": history,
            "shadow_diagnostics": shadow_diagnostics,
            "readiness": self._readiness(state, now_ms),
            "orderflow": state.last_orderflow,
        }


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
