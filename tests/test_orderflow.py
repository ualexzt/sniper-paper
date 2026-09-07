from __future__ import annotations

from sniper_paper.market import FeedClock
from sniper_paper.orderflow import DomTracker, Footprint


def test_feed_clock_disconnect_forces_reconnect_warmup_even_with_reused_connection_id() -> None:
    clock = FeedClock(gap_ms=100, warmup_ms=10)

    first = clock.observe("c1", 0)
    disconnected = clock.disconnect(5)
    reused = clock.observe("c1", 6)
    warmed = clock.observe("c1", 16)

    assert first.ready is True
    assert disconnected.connected is False
    assert disconnected.ready is False
    assert reused.ready is False
    assert reused.quarantine_reason == "reconnect_warmup"
    assert reused.quarantined_until_ms == 16
    assert warmed.ready is True


def test_footprint_buckets_are_causal_and_immutable() -> None:
    tracker = Footprint({"BTCUSDT": "0.5"}, warmup_ms=1, stack_levels=2)
    tracker.process(
        {
            "received_at_ms": 0,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {"s": "BTCUSDT", "i": "a", "S": "Sell", "p": "100", "v": "1"},
                {"s": "BTCUSDT", "i": "b", "S": "Sell", "p": "100.5", "v": "1"},
                {"s": "BTCUSDT", "i": "c", "S": "Buy", "p": "100.5", "v": "3"},
                {"s": "BTCUSDT", "i": "d", "S": "Buy", "p": "101", "v": "3"},
            ],
        }
    )
    tracker.process(
        {
            "received_at_ms": 4_000,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "b1", "S": "Sell", "p": "100.5", "v": "1"}],
        }
    )
    tracker.process(
        {
            "received_at_ms": 8_000,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "c1", "S": "Buy", "p": "100.5", "v": "3"}],
        }
    )
    tracker.process(
        {
            "received_at_ms": 12_000,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "d1", "S": "Buy", "p": "101", "v": "3"}],
        }
    )

    completed = tracker.process(
        {
            "received_at_ms": 15_001,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "e", "S": "Buy", "p": "102", "v": "1"}],
        }
    )

    assert len(completed) == 1
    bucket = completed[0]
    rows_by_price = {row["price"]: row for row in bucket["levels"]}
    assert bucket["open"] == "100"
    assert bucket["high"] == "101"
    assert bucket["low"] == "100"
    assert bucket["close"] == "101"
    assert bucket["range"] == "1"
    assert bucket["delta"] == "9"
    assert rows_by_price["100"]["sell_qty"] == "1"
    assert rows_by_price["100.5"]["sell_qty"] == "2"
    assert rows_by_price["100.5"]["buy_qty"] == "6"
    assert bucket["stacks"][0]["prices"] == ["100.5", "101"]

    tracker.process(
        {
            "received_at_ms": 17_000,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "f", "S": "Buy", "p": "103", "v": "1"}],
        }
    )
    assert bucket["close"] == "101"


def test_footprint_reconnect_marks_active_bucket_incomplete() -> None:
    tracker = Footprint({"BTCUSDT": "0.5"}, warmup_ms=1)
    tracker.process(
        {
            "received_at_ms": 0,
            "connection_id": "c1",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "a", "S": "Buy", "p": "100", "v": "1"}],
        }
    )
    tracker.process(
        {
            "received_at_ms": 3,
            "connection_id": "c2",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "b", "S": "Buy", "p": "100", "v": "1"}],
        }
    )

    completed = tracker.process(
        {
            "received_at_ms": 16_000,
            "connection_id": "c2",
            "topic": "publicTrade.BTCUSDT",
            "data": [{"s": "BTCUSDT", "i": "c", "S": "Buy", "p": "101", "v": "1"}],
        }
    )

    assert len(completed) == 1
    assert completed[0]["incomplete"] is True
    assert "reconnect" in completed[0]["incomplete_reasons"]


def test_dom_tracker_emits_persistent_wall_refill_and_reduction_labels() -> None:
    tracker = DomTracker(
        {"BTCUSDT": "20"},
        wall_multiple="0.1",
        persistence_ms=1,
        warmup_ms=1,
        max_age_ms=500,
        refill_window_ms=5,
    )
    tracker.process(
        {
            "received_at_ms": 0,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "data": {"s": "BTCUSDT", "u": 1, "b": [["100", "1"]], "a": [["101", "1"]]},
        }
    )

    persistent = tracker.process(
        {
            "received_at_ms": 2,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "data": {"s": "BTCUSDT", "u": 2, "b": [], "a": []},
        }
    )
    reduction = tracker.process(
        {
            "received_at_ms": 3,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "data": {"s": "BTCUSDT", "u": 3, "b": [["100", "0.5"]], "a": []},
        }
    )
    refill = tracker.process(
        {
            "received_at_ms": 4,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "data": {"s": "BTCUSDT", "u": 4, "b": [["100", "1"]], "a": []},
        }
    )

    assert persistent[0]["type"] == "wall_persistent"
    assert persistent[0]["uncertainty"] == "observed"
    assert reduction[0]["type"] == "depth_reduced"
    assert reduction[0]["evidence_label"] == "reduction"
    assert refill[0]["type"] == "depth_added"
    assert refill[0]["refill_candidate"] is True
    assert refill[0]["uncertainty"] == "inferred"
    assert refill[0]["feed_readiness"]["ready"] is True


def test_dom_tracker_snapshot_launches_warmup_before_ready() -> None:
    tracker = DomTracker(
        {"BTCUSDT": "20"},
        wall_multiple="0.1",
        persistence_ms=1,
        warmup_ms=10,
        max_age_ms=500,
        refill_window_ms=5,
    )
    tracker.process(
        {
            "received_at_ms": 0,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "data": {"s": "BTCUSDT", "u": 1, "b": [["100", "1"]], "a": [["101", "1"]]},
        }
    )

    prewarm = tracker.process(
        {
            "received_at_ms": 1,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "data": {"s": "BTCUSDT", "u": 2, "b": [["100", "0.5"]], "a": []},
        }
    )
    postwarm = tracker.process(
        {
            "received_at_ms": 11,
            "connection_id": "c1",
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "data": {"s": "BTCUSDT", "u": 3, "b": [["100", "0.25"]], "a": []},
        }
    )

    assert prewarm[0]["feed_readiness"]["quarantine_reason"] == "snapshot_warmup"
    assert prewarm[0]["feed_readiness"]["ready"] is False
    assert postwarm[0]["feed_readiness"]["ready"] is True
