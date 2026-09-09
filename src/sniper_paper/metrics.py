"""Causal, threshold-free metrics for completed OHLCV bars.

The functions in this module intentionally do not decide whether a value is
"good" or "bad".  They return a :class:`MetricResult` so callers can retain
the value together with sample coverage and an explanation when data is
missing or degenerate.

Conventions frozen here for this foundation:

* only bars with ``closed_at_ms <= now_ms`` are used when ``now_ms`` is given;
* ATR is the arithmetic mean of true ranges (not a Wilder/median smoother),
  with the close immediately before the requested window retained for the
  first true-range observation;
* NATR is ``mean(TR) / latest completed close * 100``;
* signed price change uses the latest close and the *open* of the bar N
  minutes earlier;
* dollar volume uses supplied quote turnover when provided, otherwise
  ``base_volume * close`` (a clearly identified p*q fallback);
* volume splash compares the current completed bar with the immediately
  preceding comparison window, excluding the current bar from the baseline;
* volatility uses population standard deviation of ``ln(close / open)``;
* BTC correlation aligns return observations by completed-bar close timestamp
  and defaults to close/previous-close returns.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import fmean, pstdev
from typing import Any


@dataclass(frozen=True)
class MetricResult:
    """A metric value plus explicit data quality information."""

    value: float | None
    samples: int
    expected_samples: int | None
    coverage: float
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


def true_range(bar: Any, previous_close: float | None = None) -> MetricResult:
    """Return true range for one bar.

    If the previous close is unavailable, the first bar's high-low range is a
    valid intrabar fallback, but the reason records the reduced convention.
    """
    try:
        high, low = float(_get(bar, "high")), float(_get(bar, "low"))
    except (KeyError, TypeError, ValueError):
        return _missing(0, 1, "missing_ohlc")
    if not all(math.isfinite(value) for value in (high, low)) or high < low:
        return _missing(0, 1, "invalid_ohlc")
    if previous_close is None:
        return MetricResult(high - low, 0, 1, 0.0, "missing_previous_close_intrabar_only")
    try:
        previous = float(previous_close)
    except (TypeError, ValueError):
        return _missing(0, 1, "invalid_previous_close")
    if not math.isfinite(previous):
        return _missing(0, 1, "invalid_previous_close")
    return MetricResult(max(high - low, abs(high - previous), abs(low - previous)), 1, 1, 1.0)


def atr(
    bars: Sequence[Any],
    period: int = 14,
    *,
    now_ms: int | None = None,
) -> MetricResult:
    """Arithmetic mean ATR over the last ``period`` completed bars.

    ``period + 1`` contiguous bars are required because the first true range
    in the requested window needs the preceding close.  Falling back to
    high-low here would no longer be the documented Digash formula.
    """
    if period < 1:
        return _missing(0, period, "invalid_period")
    completed = _completed(bars, now_ms)
    required = period + 1
    if len(completed) < required:
        return _missing(max(0, len(completed) - 1), period, "insufficient_completed_bars")
    window = completed[-required:]
    if not _contiguous(window):
        return _missing(max(0, _contiguous_count(window) - 1), period, "missing_bar_in_window")
    values: list[float] = []
    for index, bar in enumerate(window[1:], start=1):
        previous = _close(window[index - 1])
        result = true_range(bar, previous)
        if result.value is None:
            return _missing(len(values), period, result.reason or "invalid_bar")
        values.append(result.value)
    return MetricResult(fmean(values), period, period, 1.0)


def natr(
    bars: Sequence[Any],
    period: int = 14,
    *,
    now_ms: int | None = None,
    current_price: float | None = None,
) -> MetricResult:
    """Normalized ATR percentage, using the latest completed close by default."""
    atr_result = atr(bars, period, now_ms=now_ms)
    if atr_result.value is None:
        return atr_result
    completed = _completed(bars, now_ms)
    price = current_price if current_price is not None else _close(completed[-1])
    if price is None or not math.isfinite(price) or price <= 0:
        return _missing(atr_result.samples, atr_result.expected_samples, "invalid_current_price")
    return MetricResult(atr_result.value / price * 100.0, atr_result.samples, atr_result.expected_samples, atr_result.coverage, atr_result.reason)


def natr_5m_14(bars: Sequence[Any], *, now_ms: int | None = None) -> MetricResult:
    """The documented 14-bar NATR metric for a 5-minute bar series."""
    result = natr(bars, 14, now_ms=now_ms)
    completed = _completed(bars, now_ms)
    if completed and _timeframe_minutes(completed) != 5:
        return _missing(result.samples, result.expected_samples, "expected_5m_bars")
    return result


def signed_price_change(
    bars: Sequence[Any],
    minutes: int,
    *,
    now_ms: int | None = None,
) -> MetricResult:
    """Percentage change from the open N minutes earlier to latest close."""
    if minutes <= 0:
        return _missing(0, 1, "invalid_minutes")
    completed = _completed(bars, now_ms)
    if len(completed) < 2:
        return _missing(len(completed), 2, "insufficient_completed_bars")
    current = completed[-1]
    timeframe = _timeframe_minutes(completed)
    if timeframe is None or minutes % timeframe:
        return _missing(len(completed), 2, "minutes_not_aligned_to_timeframe")
    target_opened = _opened(current) - minutes * 60_000
    relevant = [bar for bar in completed if target_opened <= _opened(bar) <= _opened(current)]
    if not _contiguous(relevant):
        return _missing(_contiguous_count(relevant), 2, "missing_bar_in_window")
    base = next((bar for bar in completed if _opened(bar) == target_opened), None)
    current_close = _close(current)
    base_open = _open(base) if base is not None else None
    if base_open is None or current_close is None:
        return _missing(1, 2, "missing_n_minutes_ago_open")
    if base_open <= 0 or not math.isfinite(base_open) or not math.isfinite(current_close):
        return _missing(1, 2, "invalid_price")
    return MetricResult((current_close / base_open - 1.0) * 100.0, 2, 2, 1.0)


# The shorter alias reads naturally for callers while the long name makes the
# N-minute/open-price convention explicit in code and reports.
price_change = signed_price_change


def dollar_volume(
    bars: Sequence[Any],
    *,
    turnover: Sequence[float | None] | None = None,
    now_ms: int | None = None,
    price_field: str = "close",
) -> MetricResult:
    """Sum quote turnover or, absent it, ``volume * price_field``."""
    completed = _completed(bars, now_ms)
    if not completed:
        return _missing(0, 1, "no_completed_bars")
    if turnover is not None and len(turnover) != len(bars):
        return _missing(0, len(completed), "turnover_length_mismatch")
    total = 0.0
    for index, bar in enumerate(bars):
        if now_ms is not None and _closed(bar) > now_ms:
            continue
        if turnover is not None:
            value = turnover[index]
        else:
            volume, price = _number(bar, "volume"), _number(bar, price_field)
            value = volume * price if volume is not None and price is not None else None
        if value is None or not math.isfinite(float(value)) or float(value) < 0:
            return _missing(index, len(completed), "missing_or_invalid_turnover")
        total += float(value)
    return MetricResult(total, len(completed), len(completed), 1.0)


def volume_splash(
    bars: Sequence[Any],
    comparison_window: int = 20,
    *,
    now_ms: int | None = None,
) -> MetricResult:
    """Current volume divided by the preceding window's average bar volume.

    This is our causal adaptation of the documented splash formula: the
    current completed bar is excluded from the baseline, and both values use
    the same completed-bar duration.
    """
    if comparison_window < 1:
        return _missing(0, comparison_window + 1, "invalid_comparison_window")
    completed = _completed(bars, now_ms)
    expected = comparison_window + 1
    if len(completed) < expected:
        return _missing(len(completed), expected, "insufficient_completed_bars")
    window = completed[-expected:]
    if not _contiguous(window):
        return _missing(_contiguous_count(window), expected, "missing_bar_in_window")
    volumes = [_number(bar, "volume") for bar in window]
    if any(value is None or not math.isfinite(value) or value < 0 for value in volumes):
        return _missing(0, expected, "missing_or_invalid_volume")
    baseline = fmean(volumes[:-1])
    current = volumes[-1]
    if baseline <= 0:
        return _missing(expected, expected, "zero_baseline_volume")
    return MetricResult(current / baseline, expected, expected, 1.0)


def volatility_index(
    bars: Sequence[Any],
    window: int | None = None,
    *,
    now_ms: int | None = None,
) -> MetricResult:
    """Population stddev of completed-bar ``ln(close / open)`` returns."""
    completed = _completed(bars, now_ms)
    if window is not None and window < 1:
        return _missing(0, window, "invalid_window")
    expected = window or len(completed)
    if len(completed) < expected:
        return _missing(len(completed), expected, "insufficient_completed_bars")
    selected = completed[-expected:]
    if not _contiguous(selected):
        return _missing(_contiguous_count(selected), expected, "missing_bar_in_window")
    returns: list[float] = []
    for bar in selected:
        opening, closing = _open(bar), _close(bar)
        if opening is None or closing is None or opening <= 0 or closing <= 0:
            return _missing(len(returns), expected, "invalid_ohlc_price")
        returns.append(math.log(closing / opening))
    return MetricResult(pstdev(returns), expected, expected, 1.0)


def btc_correlation(
    coin_bars: Sequence[Any],
    btc_bars: Sequence[Any],
    *,
    return_convention: str = "close_previous_close",
    min_samples: int = 3,
    now_ms: int | None = None,
) -> MetricResult:
    """Pearson correlation of aligned coin/BTC returns, scaled to +/-100."""
    if min_samples < 2:
        return _missing(0, min_samples, "invalid_min_samples")
    convention = _normalise_return_convention(return_convention)
    if convention is None:
        return _missing(0, min_samples, "invalid_return_convention")
    coin = _completed(coin_bars, now_ms)
    btc = _completed(btc_bars, now_ms)
    coin_returns = _returns_by_timestamp(coin, convention)
    btc_returns = _returns_by_timestamp(btc, convention)
    aligned = sorted(set(coin_returns).intersection(btc_returns))
    if len(aligned) < min_samples:
        return _missing(len(aligned), min_samples, "insufficient_aligned_samples")
    x = [coin_returns[t] for t in aligned]
    y = [btc_returns[t] for t in aligned]
    x_mean, y_mean = fmean(x), fmean(y)
    x_dev = [value - x_mean for value in x]
    y_dev = [value - y_mean for value in y]
    denominator = math.sqrt(sum(value * value for value in x_dev) * sum(value * value for value in y_dev))
    if denominator == 0:
        return _missing(len(aligned), min_samples, "zero_variance")
    correlation = sum(a * b for a, b in zip(x_dev, y_dev)) / denominator
    return MetricResult(correlation * 100.0, len(aligned), min_samples, 1.0)


def _missing(samples: int, expected: int | None, reason: str) -> MetricResult:
    coverage = 0.0 if not expected else min(1.0, max(0.0, samples / expected))
    return MetricResult(None, samples, expected, coverage, reason)


def _get(bar: Any, name: str) -> Any:
    if isinstance(bar, Mapping):
        return bar[name]
    return getattr(bar, name)


def _number(bar: Any, name: str) -> float | None:
    try:
        value = float(_get(bar, name))
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _open(bar: Any) -> float | None:
    return _number(bar, "open")


def _close(bar: Any) -> float | None:
    return _number(bar, "close")


def _opened(bar: Any) -> int:
    return int(_get(bar, "opened_at_ms"))


def _closed(bar: Any) -> int:
    return int(_get(bar, "closed_at_ms"))


def _completed(bars: Sequence[Any], now_ms: int | None) -> list[Any]:
    result = []
    for bar in bars:
        try:
            if now_ms is None or _closed(bar) <= now_ms:
                result.append(bar)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=_opened)


def _timeframe_minutes(bars: Sequence[Any]) -> int | None:
    if not bars:
        return None
    try:
        timeframe_ms = int(_get(bars[0], "timeframe_ms"))
    except (KeyError, TypeError, ValueError):
        if len(bars) < 2:
            return None
        timeframe_ms = _opened(bars[1]) - _opened(bars[0])
    if timeframe_ms <= 0 or timeframe_ms % 60_000:
        return None
    return timeframe_ms // 60_000


def _contiguous(bars: Sequence[Any]) -> bool:
    if len(bars) < 2:
        return True
    timeframe = _timeframe_minutes(bars)
    if timeframe is None:
        return False
    step = timeframe * 60_000
    return all(_opened(current) - _opened(previous) == step for previous, current in pairwise(bars))


def _contiguous_count(bars: Sequence[Any]) -> int:
    if not bars:
        return 0
    count = 1
    timeframe = _timeframe_minutes(bars)
    if timeframe is None:
        return count
    step = timeframe * 60_000
    for previous, current in pairwise(bars):
        if _opened(current) - _opened(previous) != step:
            break
        count += 1
    return count


def _normalise_return_convention(value: str) -> str | None:
    normalised = value.lower().replace("/", "_").replace("-", "_")
    aliases = {
        "close_previous_close": "close_previous_close",
        "close_prev_close": "close_previous_close",
        "close_open": "close_open",
    }
    return aliases.get(normalised)


def _returns_by_timestamp(bars: Sequence[Any], convention: str) -> dict[int, float]:
    result: dict[int, float] = {}
    previous_close: float | None = None
    for bar in bars:
        opening, closing = _open(bar), _close(bar)
        if opening is None or closing is None or opening <= 0 or closing <= 0:
            previous_close = closing
            continue
        base = opening if convention == "close_open" else previous_close
        if base is not None and base > 0 and math.isfinite(base):
            result[_closed(bar)] = math.log(closing / base)
        previous_close = closing
    return result


__all__ = [
    "MetricResult",
    "atr",
    "btc_correlation",
    "dollar_volume",
    "natr",
    "natr_5m_14",
    "price_change",
    "signed_price_change",
    "true_range",
    "volatility_index",
    "volume_splash",
]
