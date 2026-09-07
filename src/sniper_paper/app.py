"""Live public-market orchestration for forward paper evaluation."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .bybit_public import BybitPublicClient
from .market import Bar, BarBuilder, Book, Trade
from .paper import PaperExecutor, Quote
from .protocol import load_protocol, protocol_sha256
from .storage import Journal
from .strategy import (
    CausalLevelEngine,
    Level,
    StrategyDecision,
    StrategyEvaluator,
    current_display_levels,
    previous_utc_day_levels,
)
from .stream import run_public_stream
from .universe import DailyUniverseSelector
from .web import start_dashboard

TIMEFRAMES = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "4h": 14_400_000}
BYBIT_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "4h": "240"}


@dataclass
class SymbolState:
    symbol: str
    book: Book = field(init=False)
    bars: dict[str, list[Bar]] = field(default_factory=lambda: {name: [] for name in TIMEFRAMES})
    builders: dict[str, BarBuilder] = field(init=False)
    level_engines: dict[str, CausalLevelEngine] = field(init=False)
    levels: list[Level] = field(default_factory=list)
    snapshot_received_at_ms: int | None = None

    def __post_init__(self) -> None:
        self.book = Book(self.symbol)
        self.builders = {name: BarBuilder(self.symbol, milliseconds) for name, milliseconds in TIMEFRAMES.items()}
        self.level_engines = {name: CausalLevelEngine(name) for name in ("15m", "4h")}


class PaperApp:
    def __init__(self, journal: Journal, client: BybitPublicClient | None = None) -> None:
        self.journal = journal
        self.client = client or BybitPublicClient()
        orphaned = self.journal.reconcile_orphaned_triggers()
        self.protocol = load_protocol()
        self.protocol_hash = protocol_sha256()
        params = self.protocol.parameters
        self.selector = DailyUniverseSelector(
            self.client,
            max_symbols=int(params["universe"]["max_symbols"]),
            candidate_pool_multiplier=int(params["universe"]["candidate_pool_multiplier"]),
            max_spread_bps=str(params["universe"]["max_spread_bps"]),
            min_depth_notional_top5=str(params["universe"]["min_depth_notional_top5"]),
        )
        self.strategy = StrategyEvaluator(**params["strategy"])
        self.strategy.restore_attempts(self.journal.attempted_level_lanes())
        self.executor = PaperExecutor(
            journal,
            equity=float(params["paper"]["initial_equity"]),
            risk_fraction=float(params["paper"]["risk_fraction"]),
            daily_loss_fraction=float(params["paper"]["daily_loss_fraction"]),
            taker_fee_rate=float(params["paper"]["taker_fee_rate"]),
            slippage_bp=float(params["paper"]["slippage_bp"]),
            latency_ms=int(params["paper"]["latency_ms"]),
        )
        self.executor.restore()
        self.max_book_age_ms = int(params["data_quality"]["max_book_age_ms"])
        self.warmup_ms = int(params["data_quality"]["warmup_minutes_after_snapshot"]) * 60_000
        self.states: dict[str, SymbolState] = {}
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
        if existing:
            symbols = [str(row["symbol"]) for row in existing]
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

        self.states = {symbol: SymbolState(symbol) for symbol in symbols}
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
        self.journal.event(_now_ms(), "INFO", "BOOTSTRAP", f"loaded causal bars for {state.symbol}")

    async def handle_message(self, message: dict, received_at_ms: int) -> None:
        if message.get("op") == "connection":
            state = str(message.get("state", "unknown"))
            self.journal.set_meta("stream_state", state)
            if state == "disconnected":
                for symbol_state in self.states.values():
                    symbol_state.book.invalidate()
                    symbol_state.snapshot_received_at_ms = None
                pending = self.executor.cancel_pending()
                if pending:
                    self.journal.update_signal_status(pending.signal_id, "MISSED", "market_stream_disconnected")
                detail = {"error": str(message.get("error", "")), "open_position": bool(self.executor.position)}
                self.journal.event(
                    received_at_ms, "WARN", "STREAM_DISCONNECTED", "public market stream disconnected", detail
                )
            else:
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
                result = self.executor.on_quote(symbol, Quote(received_at_ms, state.book.best_bid, state.book.best_ask))
                if result and result["event"] == "OPEN" and self.executor.position:
                    self.journal.update_signal_status(
                        self.executor.position.signal.signal_id, "OPEN", "paper taker entry"
                    )
                elif result and result["event"] == "CLOSE":
                    # The position is already closed; resolve the signal through the position id.
                    self._mark_closed_signal(str(result["position_id"]), str(result["exit_reason"]))
            except ValueError as exc:
                pending = self.executor.cancel_pending()
                if pending:
                    self.journal.update_signal_status(pending.signal_id, "MISSED", str(exc))
                self.journal.event(received_at_ms, "WARN", "BOOK", str(exc), {"symbol": symbol})
                raise
            return
        if topic.startswith("publicTrade."):
            for row in message.get("data", []):
                await self._trade(row, received_at_ms)

    async def _trade(self, row: dict[str, Any], received_at_ms: int) -> None:
        symbol = str(row.get("s", ""))
        state = self.states.get(symbol)
        if state is None:
            return
        try:
            trade = Trade(symbol, received_at_ms, str(row["i"]), float(row["p"]), float(row["v"]), str(row["S"]))
            completed_one = False
            for name in ("4h", "15m", "5m", "1m"):
                for completed in state.builders[name].add(trade):
                    if state.bars[name] and completed.opened_at_ms <= state.bars[name][-1].opened_at_ms:
                        continue
                    state.bars[name].append(completed)
                    self.journal.record_bar(name, completed, "public_trade")
                    state.bars[name] = state.bars[name][-500:]
                    if name in state.level_engines:
                        state.levels.extend(state.level_engines[name].add(completed))
                    completed_one = completed_one or name == "1m"
            if completed_one:
                self._evaluate(state, received_at_ms)
        except (KeyError, TypeError, ValueError) as exc:
            self.journal.event(received_at_ms, "WARN", "TRADE", str(exc), {"symbol": symbol})

    def _evaluate(self, state: SymbolState, now_ms: int) -> None:
        if not self.evaluation_eligible:
            self.journal.event(
                now_ms,
                "INFO",
                "SIGNAL_BLOCK",
                "partial UTC-day bootstrap is observation-only",
                {"symbol": state.symbol},
            )
            return
        if not state.book.healthy(now_ms, self.max_book_age_ms, 20.0):
            self.journal.event(
                now_ms, "WARN", "SIGNAL_BLOCK", "book stale or spread too wide", {"symbol": state.symbol}
            )
            return
        if state.snapshot_received_at_ms is None or now_ms - state.snapshot_received_at_ms < self.warmup_ms:
            self.journal.event(now_ms, "INFO", "SIGNAL_BLOCK", "post-snapshot warmup", {"symbol": state.symbol})
            return
        decisions = self.strategy.evaluate(
            symbol=state.symbol,
            now_ms=now_ms,
            bars=state.bars,
            levels=state.levels,
            book_imbalance=state.book.imbalance(5),
        )
        price = state.bars["1m"][-1].close
        for decision in decisions:
            self._record_decision(state, decision, price, now_ms)

    def _record_decision(self, state: SymbolState, decision: StrategyDecision, price: float, now_ms: int) -> None:
        target = decision.target
        signal = decision.signal
        signal_id = signal.signal_id if signal else uuid.uuid4().hex
        side = (
            signal.side.value if signal else ("LONG" if float(decision.features.get("trend_pct", 0)) >= 0 else "SHORT")
        )
        features = dict(decision.features)
        if target:
            features["level_id"] = target.level_id
        row = {
            "signal_id": signal_id,
            "occurred_at_ms": now_ms,
            "symbol": state.symbol,
            "side": side,
            "lane": decision.lane,
            "target_timeframe": target.timeframe if target else "none",
            "level_class": target.level_class if target else "none",
            "trigger_price": price,
            "stop_price": signal.stop_price if signal else price,
            "target_price": signal.target_price if signal else (target.price if target else price),
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
                self.executor.submit(signal)

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
        bars = state.bars[timeframe][-180:]
        last_price = bars[-1].close if bars else None
        levels = current_display_levels(state.levels, symbol, last_price, _now_ms()) if last_price is not None else []
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
                    "time": _stamp(pending.occurred_at_ms),
                    "symbol": pending.symbol,
                    "type": "ENTRY",
                    "side": pending.side.value,
                    "price": "next executable quote",
                    "status": "TRIGGERED",
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
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": [asdict(bar) for bar in bars],
            "levels": [asdict(level) for level in levels],
            "quote": quote,
            "last_price": last_price,
            "positions": positions,
            "open_orders": open_orders,
            "history": history,
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
