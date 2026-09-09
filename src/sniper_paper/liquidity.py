"""Pure, execution-agnostic liquidity and cascade geometry calculations.

The functions in this module do not decide whether a quote is acceptable.  In
particular, they deliberately contain no spread, impact, or depth thresholds.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

Side = Literal["buy", "sell"]
Reference = Literal["mid", "best"]
PriceSize = tuple[float, float]


@dataclass(frozen=True, slots=True)
class LiquidityImpact:
    side: Side
    reference: Reference
    quote_notional: float
    reference_price: float | None
    vwap_price: float | None
    impact_bps: float | None
    marginal_impact_bps: float | None
    consumed_base_qty: float
    consumed_notional: float
    complete_depth: bool
    reason: str


def calculate_liquidity_impact(
    bids: Iterable[PriceSize],
    asks: Iterable[PriceSize],
    quote_notional: float,
    *,
    side: Side,
    reference: Reference = "mid",
) -> LiquidityImpact:
    """Calculate quote-sized VWAP and marginal adverse impact.

    ``quote_notional`` is the actual quote currency size to consume.  A buy
    consumes asks (ascending price), while a sell consumes bids (descending
    price).  VWAP impact is adverse and therefore non-negative for a valid
    non-crossed book.  ``marginal_impact_bps`` is the adverse displacement of
    the last consumed price level from the selected reference price.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if reference not in ("mid", "best"):
        raise ValueError("reference must be 'mid' or 'best'")
    if not math.isfinite(quote_notional) or quote_notional <= 0:
        return _empty(side, reference, quote_notional, "invalid_quote_notional")

    try:
        bid_levels = _normalise(bids, descending=True)
        ask_levels = _normalise(asks, descending=False)
    except ValueError as exc:
        return _empty(side, reference, quote_notional, str(exc))
    if not bid_levels or not ask_levels:
        return _empty(side, reference, quote_notional, "empty_book")
    if bid_levels[0][0] >= ask_levels[0][0]:
        return _empty(side, reference, quote_notional, "crossed_or_locked_book")

    best_bid, best_ask = bid_levels[0][0], ask_levels[0][0]
    reference_price = (best_bid + best_ask) / 2 if reference == "mid" else (best_ask if side == "buy" else best_bid)
    levels = ask_levels if side == "buy" else bid_levels
    consumed_base = 0.0
    consumed_notional = 0.0
    marginal_price: float | None = None
    remaining = quote_notional
    for price, size in levels:
        level_notional = price * size
        take_notional = min(remaining, level_notional)
        take_base = take_notional / price
        consumed_base += take_base
        consumed_notional += take_notional
        marginal_price = price
        remaining -= take_notional
        if remaining <= max(1e-12, quote_notional * 1e-12):
            remaining = 0.0
            break

    complete = remaining == 0.0
    # Execution VWAP is total quote divided by total base.  Weighting prices
    # by quote notional would be subtly wrong when the requested size itself
    # is expressed in quote currency.
    vwap = consumed_notional / consumed_base if consumed_base > 0 else None
    impact = _adverse_bps(vwap, reference_price, side) if vwap is not None else None
    marginal = _adverse_bps(marginal_price, reference_price, side) if marginal_price is not None else None
    return LiquidityImpact(
        side=side,
        reference=reference,
        quote_notional=quote_notional,
        reference_price=reference_price,
        vwap_price=vwap,
        impact_bps=impact,
        marginal_impact_bps=marginal,
        consumed_base_qty=consumed_base,
        consumed_notional=consumed_notional,
        complete_depth=complete,
        reason="ok" if complete else "insufficient_depth",
    )


def adjacent_price_gaps(prices: Iterable[float]) -> tuple[float, ...]:
    """Return ascending adjacent gaps, preserving duplicate-price zero gaps."""
    values = _finite_prices(prices)
    ordered = sorted(values)
    return tuple(b - a for a, b in pairwise(ordered))


def total_price_span(prices: Iterable[float]) -> float:
    """Return max-minus-min price, or zero for an empty/singleton set."""
    values = _finite_prices(prices)
    return max(values) - min(values) if values else 0.0


def _normalise(levels: Iterable[PriceSize], *, descending: bool) -> list[PriceSize]:
    result: list[PriceSize] = []
    for level in levels:
        try:
            price_raw, size_raw = level
            price, size = float(price_raw), float(size_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_level_shape") from exc
        if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
            raise ValueError("invalid_level")
        result.append((price, size))
    return sorted(result, key=lambda item: item[0], reverse=descending)


def _finite_prices(prices: Iterable[float]) -> list[float]:
    try:
        values = [float(price) for price in prices]
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_price") from exc
    if any(not math.isfinite(price) or price <= 0 for price in values):
        raise ValueError("invalid_price")
    return values


def _adverse_bps(price: float | None, reference: float, side: Side) -> float | None:
    if price is None:
        return None
    return ((price / reference - 1) if side == "buy" else (1 - price / reference)) * 10_000


def _empty(side: Side, reference: Reference, quote_notional: float, reason: str) -> LiquidityImpact:
    return LiquidityImpact(side, reference, quote_notional, None, None, None, None, 0.0, 0.0, False, reason)
