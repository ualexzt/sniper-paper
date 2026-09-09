from __future__ import annotations

import math

import pytest

from sniper_paper.liquidity import adjacent_price_gaps, calculate_liquidity_impact, total_price_span

BOOK_BIDS = [(99.0, 2.0), (98.0, 3.0)]
BOOK_ASKS = [(101.0, 1.0), (102.0, 2.0)]


def test_buy_uses_sorted_asks_and_mid_reference_with_partial_last_level() -> None:
    result = calculate_liquidity_impact(
        BOOK_BIDS,
        [(102.0, 2.0), (101.0, 1.0)],
        150.0,
        side="buy",
        reference="mid",
    )

    assert result.complete_depth is True
    assert result.reason == "ok"
    assert result.reference_price == pytest.approx(100.0)
    assert result.consumed_base_qty == pytest.approx(101 / 101 + 49 / 102)
    assert result.consumed_notional == pytest.approx(150.0)
    assert result.vwap_price == pytest.approx(result.consumed_notional / result.consumed_base_qty)
    assert result.impact_bps == pytest.approx((result.vwap_price / 100 - 1) * 10_000)
    assert result.marginal_impact_bps == pytest.approx(200.0)


def test_sell_uses_descending_bids_and_best_reference() -> None:
    result = calculate_liquidity_impact(BOOK_BIDS, BOOK_ASKS, 250.0, side="sell", reference="best")

    assert result.reference_price == 99.0
    assert result.consumed_base_qty == pytest.approx(2 + 52 / 98)
    assert result.vwap_price == pytest.approx(result.consumed_notional / result.consumed_base_qty)
    assert result.marginal_impact_bps == pytest.approx((1 - 98 / 99) * 10_000)


def test_insufficient_depth_reports_consumed_amount_and_reason() -> None:
    result = calculate_liquidity_impact(BOOK_BIDS, BOOK_ASKS, 500.0, side="buy")

    assert result.complete_depth is False
    assert result.reason == "insufficient_depth"
    assert result.consumed_notional == pytest.approx(305.0)
    assert result.consumed_base_qty == pytest.approx(1 + 2)


@pytest.mark.parametrize(
    ("bids", "asks", "reason"),
    [([], BOOK_ASKS, "empty_book"), ([(99.0, 0.0)], BOOK_ASKS, "invalid_level"), ([(101.0, 1.0)], BOOK_ASKS, "crossed_or_locked_book")],
)
def test_invalid_and_crossed_books_fail_closed(bids, asks, reason: str) -> None:
    result = calculate_liquidity_impact(bids, asks, 10.0, side="buy")
    assert result.complete_depth is False
    assert result.reason == reason
    assert result.vwap_price is None


def test_invalid_quote_is_reported_without_division() -> None:
    result = calculate_liquidity_impact(BOOK_BIDS, BOOK_ASKS, math.nan, side="sell")
    assert result.reason == "invalid_quote_notional"
    assert result.consumed_notional == 0.0


def test_cascade_gaps_and_total_span_are_sorted_and_threshold_free() -> None:
    assert adjacent_price_gaps([102.0, 100.0, 100.0, 105.0]) == (0.0, 2.0, 3.0)
    assert total_price_span([102.0, 100.0, 105.0]) == 5.0
    assert total_price_span([]) == 0.0
