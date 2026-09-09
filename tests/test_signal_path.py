from __future__ import annotations

import pytest

from sniper_paper.signal_path import CompletedBar, IncrementalSignalPath, PathStatus, SignalPathTracker, TradeTick


def tracker(side: str = "buy") -> SignalPathTracker:
    return SignalPathTracker(
        "sig-1", side=side, entry_price=100.0, target_price=103.0 if side == "buy" else 97.0,
        stop_price=98.0 if side == "buy" else 102.0, start_at_ms=1_000,
    )


def test_ordered_trades_resolve_first_touch_and_excursions() -> None:
    result = tracker().evaluate(
        trades=(TradeTick(1_100, 99.0), TradeTick(1_200, 101.0), TradeTick(1_300, 103.2)),
        timeout_at_ms=2_000,
    )
    assert result.status is PathStatus.TP_TOUCHED
    assert result.first_touch_at_ms == 1_300
    assert result.first_touch_price == 103.2
    assert result.mfe_bps == pytest.approx(320.0)
    assert result.mae_bps == pytest.approx(-100.0)
    assert "fill" not in result.to_dict()
    assert "pnl" not in result.to_dict()


def test_bar_touch_of_both_boundaries_is_ambiguous() -> None:
    result = tracker().evaluate(bars=(CompletedBar(1_000, 2_000, 104.0, 97.0),))
    assert result.status is PathStatus.AMBIGUOUS
    assert result.first_touch_at_ms == 2_000
    assert result.coverage_complete is True


def test_trade_order_can_resolve_what_bar_cannot() -> None:
    result = tracker().evaluate(
        trades=(TradeTick(1_500, 98.0), TradeTick(1_600, 103.0)),
        bars=(CompletedBar(1_000, 2_000, 104.0, 97.0),),
    )
    assert result.status is PathStatus.SL_TOUCHED
    assert result.first_touch_at_ms == 1_500


def test_incomplete_coverage_is_unknown_even_if_observed_path_touched() -> None:
    result = tracker().evaluate(
        trades=(TradeTick(1_100, 103.0),), coverage_complete=False, coverage_reason="book_gap"
    )
    assert result.status is PathStatus.UNKNOWN
    assert result.coverage_reason == "book_gap"
    assert result.covered_until_ms == 1_100


def test_incomplete_bar_marks_coverage_unknown() -> None:
    result = tracker().evaluate(bars=(CompletedBar(1_000, 2_000, 104.0, 99.0, complete=False),))
    assert result.status is PathStatus.UNKNOWN
    assert result.coverage_reason == "incomplete_bar"


def test_bar_overlapping_signal_start_cannot_use_pre_signal_range() -> None:
    result = tracker().evaluate(bars=(CompletedBar(900, 1_100, 104.0, 99.0),))

    assert result.status is PathStatus.UNKNOWN
    assert result.coverage_reason == "bar_overlaps_signal_start"
    assert result.first_touch_at_ms is None


def test_timeout_and_short_side_are_causal() -> None:
    timeout = tracker().evaluate(trades=(TradeTick(1_100, 100.5), TradeTick(2_000, 100.5)), timeout_at_ms=2_000)
    assert timeout.status is PathStatus.TIMEOUT
    short = tracker("sell").evaluate(trades=(TradeTick(1_100, 101.0), TradeTick(1_200, 96.5)))
    assert short.status is PathStatus.TP_TOUCHED
    assert short.first_touch_price == 96.5


def test_invalid_bar_and_price_contracts_fail_closed() -> None:
    with pytest.raises(ValueError, match="invalid completed bar"):
        tracker().evaluate(bars=(CompletedBar(1_000, 900, 101.0, 99.0),))
    with pytest.raises(ValueError, match="prices"):
        SignalPathTracker("x", side="buy", entry_price=100, target_price=99, stop_price=98, start_at_ms=0)


def test_incremental_tracker_keeps_extrema_and_first_touch_without_tick_history() -> None:
    live = IncrementalSignalPath(tracker())

    assert live.observe_trade(TradeTick(1_100, 99.0)).status is PathStatus.NO_TOUCH
    assert live.observe_trade(TradeTick(1_200, 101.0)).mfe_bps == pytest.approx(100.0)
    terminal = live.observe_trade(TradeTick(1_300, 103.2))

    assert terminal.status is PathStatus.TP_TOUCHED
    assert terminal.mae_bps == pytest.approx(-100.0)
    assert live.observe_trade(TradeTick(1_400, 97.0)) == terminal


def test_incremental_tracker_fails_closed_on_gap() -> None:
    live = IncrementalSignalPath(tracker())
    live.observe_trade(TradeTick(1_100, 101.0))

    result = live.mark_unknown(1_200, "stream_disconnected")

    assert result.status is PathStatus.UNKNOWN
    assert result.coverage_complete is False
