import math

import pytest

from sniper_paper.metrics import (
    atr,
    btc_correlation,
    dollar_volume,
    natr_5m_14,
    signed_price_change,
    true_range,
    volatility_index,
    volume_splash,
)


def bar(
    opened: int,
    *,
    opening: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: float = 10.0,
    step: int = 60_000,
) -> dict:
    return {
        "opened_at_ms": opened,
        "closed_at_ms": opened + step,
        "timeframe_ms": step,
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_true_range_uses_gap_and_records_first_bar_fallback() -> None:
    current = bar(0, high=100, low=95)
    assert true_range(current, 90).value == 10
    first = true_range(current)
    assert first.value == 5
    assert first.reason == "missing_previous_close_intrabar_only"
    assert first.coverage == 0


def test_atr_is_arithmetic_mean_and_only_completed_bars_are_used() -> None:
    bars = [
        bar(0, high=91, low=89, close=90),
        bar(60_000, high=101, low=99, close=100),
        bar(120_000, high=104, low=100, close=103),
        bar(180_000, high=108, low=102, close=107),
        bar(240_000, high=120, low=110, close=115),  # forming at now=240_000
    ]
    result = atr(bars, 3, now_ms=240_000)
    # The close before the three-bar window participates in the first TR.
    assert result.value == pytest.approx((11 + 4 + 6) / 3)
    assert result.samples == result.expected_samples == 3

    gap = [bars[0], bars[1], bars[3], bars[4]]
    assert atr(gap, 3).reason == "missing_bar_in_window"


def test_natr_5m_14_is_mean_tr_over_latest_completed_close() -> None:
    bars = [
        bar(index * 300_000, high=101, low=99, close=100, step=300_000)
        for index in range(15)
    ]
    result = natr_5m_14(bars)
    assert result.value == pytest.approx(2.0)
    assert result.coverage == 1.0
    wrong_timeframe = natr_5m_14([bar(index * 60_000) for index in range(15)])
    assert wrong_timeframe.value is None
    assert wrong_timeframe.reason == "expected_5m_bars"


def test_signed_price_change_uses_open_n_minutes_ago() -> None:
    bars = [
        bar(0, opening=100, close=101, step=300_000),
        bar(300_000, opening=110, close=111, step=300_000),
        bar(600_000, opening=120, close=132, step=300_000),
    ]
    result = signed_price_change(bars, 10)
    assert result.value == pytest.approx(32.0)
    assert signed_price_change(bars, 7).reason == "minutes_not_aligned_to_timeframe"


def test_dollar_volume_prefers_supplied_turnover_and_has_explicit_pq_fallback() -> None:
    bars = [bar(0, close=10, volume=2), bar(60_000, close=12, volume=3)]
    assert dollar_volume(bars, turnover=[50, 70]).value == 120
    assert dollar_volume(bars).value == 56
    assert dollar_volume(bars, turnover=[50]).reason == "turnover_length_mismatch"
    assert dollar_volume([]).reason == "no_completed_bars"


def test_volume_splash_excludes_current_bar_from_preceding_baseline() -> None:
    bars = [bar(index * 60_000, volume=10) for index in range(3)]
    bars.append(bar(180_000, volume=40))
    result = volume_splash(bars, comparison_window=3)
    assert result.value == 4
    assert result.reason is None
    assert volume_splash(bars[:2], comparison_window=3).reason == "insufficient_completed_bars"


def test_volatility_index_is_population_stddev_of_log_close_open() -> None:
    bars = [
        bar(0, opening=100, close=110),
        bar(60_000, opening=100, close=100),
        bar(120_000, opening=100, close=90),
    ]
    values = (math.log(1.1), 0.0, math.log(0.9))
    mean = sum(values) / len(values)
    expected = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    assert volatility_index(bars).value == pytest.approx(expected)
    assert volatility_index([bars[0], bars[2]], window=2).reason == "missing_bar_in_window"


def test_btc_correlation_aligns_timestamps_supports_conventions_and_none_variance() -> None:
    # The first close/previous-close return has no prior close; three later
    # timestamps are aligned despite the coin input arriving in reverse order.
    coin = [
        bar(180_000, close=108),
        bar(0, close=100),
        bar(120_000, close=104),
        bar(60_000, close=102),
    ]
    btc = [
        bar(0, close=200),
        bar(60_000, close=204),
        bar(120_000, close=208),
        bar(180_000, close=216),
    ]
    result = btc_correlation(coin, btc)
    assert result.value == pytest.approx(100.0)
    assert result.samples == 3
    assert btc_correlation(coin, btc, return_convention="close/open").value is not None
    flat = [bar(index * 60_000, close=100) for index in range(4)]
    assert btc_correlation(flat, btc).value is None
    assert btc_correlation(flat, btc).reason == "zero_variance"
    assert btc_correlation(coin[:2], btc[:2], min_samples=3).reason == "insufficient_aligned_samples"
