from sniper_paper.paper import OpenPaperPosition, PaperSignal, Quote, Side
from sniper_paper.profit_protection import ProfitProtection, ProfitProtectionConfig
from sniper_paper.storage import Journal


def _position() -> OpenPaperPosition:
    signal = PaperSignal("s1", 1, "BTCUSDT", Side.LONG, "lane", 99.0, 105.0)
    return OpenPaperPosition("p1", signal, 2, 100.0, 1.0, 0.02)


def test_protection_uses_net_r_and_monotonic_floor() -> None:
    guard = ProfitProtection(_position(), taker_fee_rate=0.00055, slippage_bp=2.0,
                             config=ProfitProtectionConfig(activation_r=1.0, giveback_r=0.5, delta_baseline_count=1))
    assert guard.on_quote(Quote(3, 101.2, 101.3)) == []
    assert guard.active
    floor = guard.protected_floor
    assert floor > 0
    old_risk = guard.risk_amount
    guard.on_quote(Quote(4, 100.8, 100.9))
    assert guard.protected_floor >= floor
    assert guard.on_quote(Quote(5, 100.5, 100.6))[0]["decision"] == "SHADOW_EXIT"
    guard.sync_position(OpenPaperPosition("p1", _position().signal, 2, 100.0, 2.0, 0.04))
    assert all(row["mode"] != "price_only" for row in guard.on_quote(Quote(6, 100.4, 100.5)))
    assert guard.quantity == 2.0 and guard.entry_fee == 0.04
    assert guard.risk_amount > old_risk and guard.protected_floor >= floor


def test_orderflow_requires_complete_adverse_delta_and_price_loss() -> None:
    guard = ProfitProtection(_position(), taker_fee_rate=0.00055, slippage_bp=2.0,
                             config=ProfitProtectionConfig(activation_r=1.0, giveback_r=0.5, delta_baseline_count=1))
    quote = Quote(3, 101.2, 101.3)
    guard.on_quote(quote)
    row = {"bucket_start_ms": 0, "bucket_end_ms": 15_000, "delta_notional": -10.0,
           "incomplete": False, "partial": False, "feed_readiness": {"ready": True}}
    first = guard.on_footprint({**row, "low": 100.4, "high": 101.0, "close": 100.8}, Quote(4, 100.5, 100.6))
    assert [item["mode"] for item in first] == ["price_only", "price_plus_orderflow"]
    assert first[1]["decision"] == "WAIT_ORDERFLOW"
    second = guard.on_footprint({**row, "bucket_end_ms": 30_000, "delta_notional": -30.0, "low": 99.0, "high": 100.0, "close": 99.0}, Quote(5, 100.5, 100.6))
    assert len(second) == 1 and second[0]["mode"] == "price_plus_orderflow" and second[0]["decision"] == "SHADOW_EXIT"
    assert guard.on_footprint({**row, "bucket_end_ms": 30_000, "incomplete": True}, quote) == []


def test_profit_protection_rejects_other_symbol_quote_and_footprint() -> None:
    guard = ProfitProtection(_position(), taker_fee_rate=0.00055, slippage_bp=2.0,
                             config=ProfitProtectionConfig(activation_r=1.0, giveback_r=0.5))
    other_quote = Quote(3, 101.2, 101.3, symbol="ETHUSDT")
    assert guard.on_quote(other_quote) == []
    assert guard.on_footprint(
        {"symbol": "ETHUSDT", "bucket_end_ms": 15_000, "delta_notional": -100.0,
         "feed_readiness": {"ready": True}},
        Quote(4, 100.5, 100.6, symbol="ETHUSDT"),
    ) == []


def test_shadow_events_are_auditable_and_idempotent(tmp_path) -> None:
    journal = Journal(tmp_path / "paper.db")
    event = {"position_id": "p1", "occurred_at_ms": 10, "mode": "price_only",
             "decision": "SHADOW_EXIT", "reason": "protected_floor", "net_pnl": 1.0}
    journal.record_profit_shadow_event(event, "hash")
    journal.record_profit_shadow_event(event, "hash")
    journal.record_profit_shadow_event({**event, "position_id": "p2", "occurred_at_ms": 11}, "hash")
    journal.record_profit_shadow_event({**event, "position_id": "p1", "occurred_at_ms": 12}, "other")
    rows = journal.profit_shadow_history()
    assert len(rows) == 3
    assert rows[0]["data"]["net_pnl"] == 1.0
    assert len(journal.profit_shadow_history(position_id="p2", protocol_hash="hash")) == 1
    assert journal.profit_shadow_history(position_id="p1", protocol_hash="hash")[0]["position_id"] == "p1"


def test_baseline_close_settles_only_nonterminal_modes() -> None:
    guard = ProfitProtection(_position(), taker_fee_rate=0.00055, slippage_bp=2.0,
                             config=ProfitProtectionConfig())
    guard.on_quote(Quote(3, 101.2, 101.3))
    guard.on_quote(Quote(4, 100.5, 100.6))
    rows = guard.on_baseline_close({"closed_at_ms": 5, "exit_price": 100.0, "exit_reason": "TP", "net_pnl": 1.2})
    assert {row["mode"] for row in rows} == {"price_plus_orderflow"}
    assert rows[0]["decision"] == "BASELINE_EXIT"
