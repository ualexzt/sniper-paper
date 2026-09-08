from __future__ import annotations

import asyncio
from pathlib import Path

from sniper_paper.app import PaperApp, SymbolState
from sniper_paper.market import Bar
from sniper_paper.paper import Side as PaperSide
from sniper_paper.shadow_setups import (
    RetestReclaimShadowSetup,
    ShadowSetupPhase,
    ShadowSetupStatus,
)
from sniper_paper.storage import Journal
from sniper_paper.strategy import Level, LevelSide
from sniper_paper.strategy_v2 import Level as V2Level
from sniper_paper.strategy_v2 import LevelSide as V2LevelSide


class _FakeExecutor:
    pending = None

    def __init__(self) -> None:
        self.quotes: list[tuple[str, object]] = []

    def on_quote(self, symbol: str, quote: object) -> None:
        self.quotes.append((symbol, quote))


class _FakeDom:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self._responses = responses

    def process(self, row: dict[str, object]) -> list[dict[str, object]]:
        if not self._responses:
            return []
        return self._responses.pop(0)


class _FakeRetestEvaluator:
    def __init__(self, setups: list[RetestReclaimShadowSetup]) -> None:
        self.setups = setups
        self.calls: list[dict[str, object]] = []

    def evaluate(self, **kwargs: object) -> list[RetestReclaimShadowSetup]:
        self.calls.append(dict(kwargs))
        return self.setups


def _app(tmp_path: Path) -> PaperApp:
    return PaperApp(Journal(tmp_path / "paper.db"))


def test_update_density_walls_snapshot_lifecycle(tmp_path: Path) -> None:
    app = _app(tmp_path)
    state = SymbolState("BTCUSDT", tick_size=0.01)
    state.density_walls[("bid", 100.0)] = {
        "side": "bid",
        "price": 100.0,
        "observed_at_ms": 1,
        "initial_size": 9.0,
        "current_remaining": 9.0,
        "source_event_id": "stale",
        "evidence_quality": "observed",
    }
    app.states = {"BTCUSDT": state}
    app.executor = _FakeExecutor()
    app.dom = _FakeDom(
        [
            [
                {
                    "type": "wall_persistent",
                    "side": "bid",
                    "price": 101.0,
                    "quantity": 7.0,
                    "received_ms": 1_000,
                    "first_candidate_at_ms": 900,
                }
            ],
            [
                {
                    "type": "depth_reduced",
                    "side": "bid",
                    "price": 101.0,
                    "quantity": 4.0,
                    "received_ms": 1_500,
                }
            ],
        ]
    )

    asyncio.run(
        app.handle_message(
            {
                "topic": "orderbook.50.BTCUSDT",
                "type": "snapshot",
                "data": {
                    "s": "BTCUSDT",
                    "u": 1,
                    "seq": 1,
                    "b": [["99.90", "2"]],
                    "a": [["100.10", "3"]],
                },
            },
            1_000,
        )
    )

    assert ("bid", 100.0) not in state.density_walls
    assert state.density_walls[("bid", 101.0)]["current_remaining"] == 7.0
    assert state.density_walls[("bid", 101.0)]["evidence_quality"] == "observed"

    asyncio.run(
        app.handle_message(
            {
                "topic": "orderbook.50.BTCUSDT",
                "type": "delta",
                "data": {
                    "s": "BTCUSDT",
                    "u": 2,
                    "seq": 2,
                    "b": [["99.89", "2"]],
                    "a": [["100.11", "3"]],
                },
            },
            1_500,
        )
    )

    assert state.density_walls[("bid", 101.0)]["current_remaining"] == 4.0
    assert state.snapshot_received_at_ms == 1_000
    assert len(app.executor.quotes) == 2


def test_evaluate_shadow_writes_only_shadow_diagnostics(tmp_path: Path) -> None:
    app = _app(tmp_path)
    state = SymbolState("BTCUSDT")
    state.bars["1m"] = [Bar("BTCUSDT", 60_000, 0, 60_000, 99.0, 101.0, 98.5, 100.2, 1.0, 0.0, 1)]
    state.levels = [Level("l1", "BTCUSDT", "15m", LevelSide.HIGH, 100.0, 0, broken_at_ms=60_000)]
    app.states = {"BTCUSDT": state}
    app.shadow_retests = {
        "BTCUSDT": _FakeRetestEvaluator(
            [
                RetestReclaimShadowSetup(
                    setup_id="setup-1",
                    symbol="BTCUSDT",
                    lane="retest_reclaim_v1",
                    side=PaperSide.LONG,
                    level_id="l1",
                    level_timeframe="15m",
                    level_price=100.0,
                    level_confirmed_at_ms=0,
                    broken_at_ms=120_000,
                    retest_at_ms=180_000,
                    reclaim_at_ms=240_000,
                    invalidated_at_ms=None,
                    status=ShadowSetupStatus.CONFIRMED,
                    phase=ShadowSetupPhase.RECLAIMED,
                    reason="reclaimed",
                    entry_price=101.0,
                    stop_price=99.0,
                    target_price=105.0,
                    features={"level_id": "l1"},
                )
            ]
        )
    }
    app.shadow_orderflow = {}
    app.journal.record_signal = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected signal"))
    app.journal.record_paper_order = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected order"))
    app.journal.open_position = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected position"))

    app._evaluate_shadow(
        state,
        [V2Level("l1", "BTCUSDT", "15m", V2LevelSide.HIGH, 100.0, 0, broken_at_ms=60_000)],
        240_000,
    )

    with app.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_diagnostics").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_market_detail_exposes_symbol_scoped_shadow_diagnostics(tmp_path: Path) -> None:
    app = _app(tmp_path)
    state = SymbolState("BTCUSDT")
    state.bars["5m"] = [Bar("BTCUSDT", 300_000, 0, 300_000, 100.0, 102.0, 99.0, 101.0, 1.0, 0.0, 1)]
    state.levels = [Level("l1", "BTCUSDT", "4h", LevelSide.HIGH, 110.0, 1)]
    app.states = {"BTCUSDT": state}

    app.journal.record_shadow_diagnostic(
        {
            "diagnostic_id": "btc-1",
            "setup_id": "setup-1",
            "occurred_at_ms": 1_000,
            "symbol": "BTCUSDT",
            "lane": "retest_reclaim_v1",
            "side": "LONG",
            "status": "CONFIRMED",
            "reason": "reclaimed",
            "reference_price": 100.0,
            "stop_price": 99.0,
            "target_price": 105.0,
            "protocol_hash": "shadow",
            "features": {"level_id": "l1"},
        }
    )
    app.journal.record_shadow_diagnostic(
        {
            "diagnostic_id": "eth-1",
            "setup_id": "setup-2",
            "occurred_at_ms": 2_000,
            "symbol": "ETHUSDT",
            "lane": "retest_reclaim_v1",
            "side": "LONG",
            "status": "CONFIRMED",
            "reason": "reclaimed",
            "reference_price": 200.0,
            "stop_price": 199.0,
            "target_price": 205.0,
            "protocol_hash": "shadow",
            "features": {"level_id": "l2"},
        }
    )

    result = app.market_detail("BTCUSDT", "5m")

    assert {row["symbol"] for row in result["shadow_diagnostics"]} == {"BTCUSDT"}
    assert result["shadow_diagnostics"][0]["features"] == {"level_id": "l1"}
