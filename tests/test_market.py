import pytest

from sniper_paper.market import BarBuilder, Book, Trade


def test_book_snapshot_delta_delete_and_health() -> None:
    book = Book("BTCUSDT")
    book.apply(
        {"type": "snapshot", "data": {"s": "BTCUSDT", "u": 5, "seq": 50, "b": [["100", "2"]], "a": [["101", "3"]]}},
        1_000,
    )
    assert book.best_bid == 100
    book.apply(
        {"type": "delta", "data": {"s": "BTCUSDT", "u": 6, "seq": 51, "b": [["100", "0"], ["100.5", "1"]], "a": []}},
        1_010,
    )
    assert book.best_bid == 100.5
    assert book.healthy(1_100, 500, 60)
    assert not book.healthy(2_000, 500, 60)


def test_book_rejects_delta_before_snapshot_and_nonincreasing_update() -> None:
    book = Book("BTCUSDT")
    with pytest.raises(ValueError):
        book.apply({"type": "delta", "data": {"s": "BTCUSDT", "u": 2, "seq": 2, "b": [], "a": []}}, 1)
    book.apply(
        {"type": "snapshot", "data": {"s": "BTCUSDT", "u": 2, "seq": 2, "b": [["1", "1"]], "a": [["2", "1"]]}}, 2
    )
    with pytest.raises(ValueError):
        book.apply({"type": "delta", "data": {"s": "BTCUSDT", "u": 2, "seq": 3, "b": [], "a": []}}, 3)
    assert not book.ready


def test_book_invalidate_requires_a_fresh_snapshot() -> None:
    book = Book("BTCUSDT")
    book.apply(
        {"type": "snapshot", "data": {"s": "BTCUSDT", "u": 2, "seq": 2, "b": [["1", "1"]], "a": [["2", "1"]]}}, 2
    )
    book.invalidate()
    with pytest.raises(ValueError):
        book.apply({"type": "delta", "data": {"s": "BTCUSDT", "u": 3, "seq": 3, "b": [], "a": []}}, 3)


def test_bar_builder_is_receive_time_causal_and_deduplicates() -> None:
    builder = BarBuilder("BTCUSDT", 60_000)
    first = Trade("BTCUSDT", 1_000, "a", 100, 2, "Sell")
    second = Trade("BTCUSDT", 2_000, "b", 101, 1, "Buy")
    assert builder.add(first) == []
    assert builder.add(first) == []
    assert builder.add(second) == []
    bars = builder.add(Trade("BTCUSDT", 61_000, "c", 102, 1, "Buy"))
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.trades) == (100, 101, 100, 101, 2)
    assert bar.delta_notional == -99


def test_bar_builder_exposes_forming_snapshot_without_completing_it() -> None:
    builder = BarBuilder("BTCUSDT", 60_000)
    builder.add(Trade("BTCUSDT", 1_000, "a", 100, 2, "Sell"))
    builder.add(Trade("BTCUSDT", 2_000, "b", 103, 1, "Buy"))

    forming = builder.current()

    assert forming is not None
    assert (forming.open, forming.high, forming.low, forming.close, forming.trades) == (100, 103, 100, 103, 2)
    assert builder.current() == forming


def test_bar_builder_rejects_late_receive_time() -> None:
    builder = BarBuilder("ETHUSDT", 60_000)
    builder.add(Trade("ETHUSDT", 61_000, "a", 10, 1, "Buy"))
    with pytest.raises(ValueError):
        builder.add(Trade("ETHUSDT", 59_000, "b", 10, 1, "Buy"))
