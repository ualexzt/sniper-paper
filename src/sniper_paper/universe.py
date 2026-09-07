from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .bybit_public import BybitPublicClient, BybitPublicError


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _rank_scores(values: list[tuple[str, Decimal]], *, descending: bool) -> dict[str, Decimal]:
    if not values:
        return {}
    ordered = sorted(
        values,
        key=lambda item: ((-item[1]) if descending else item[1], item[0]),
    )
    if len(ordered) == 1:
        symbol, _ = ordered[0]
        return {symbol: Decimal(1)}
    total = Decimal(len(ordered) - 1)
    scores: dict[str, Decimal] = {}
    for rank, (symbol, _) in enumerate(ordered):
        scores[symbol] = Decimal(len(ordered) - 1 - rank) / total
    return scores


@dataclass(frozen=True)
class UniverseExclusion:
    symbol: str
    reasons: tuple[str, ...]
    turnover24h: Decimal | None = None
    volume24h: Decimal | None = None
    abs_price24h_pcnt: Decimal | None = None
    spread_bps: Decimal | None = None
    depth_notional_top5: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reasons": list(self.reasons),
            "turnover24h": _decimal_to_json(self.turnover24h),
            "volume24h": _decimal_to_json(self.volume24h),
            "abs_price24h_pcnt": _decimal_to_json(self.abs_price24h_pcnt),
            "spread_bps": _decimal_to_json(self.spread_bps),
            "depth_notional_top5": _decimal_to_json(self.depth_notional_top5),
        }


@dataclass(frozen=True)
class UniverseSelection:
    symbol: str
    rank: int
    score: Decimal
    turnover24h: Decimal
    volume24h: Decimal
    abs_price24h_pcnt: Decimal
    spread_bps: Decimal
    depth_notional_top5: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "rank": self.rank,
            "score": _decimal_to_json(self.score),
            "turnover24h": _decimal_to_json(self.turnover24h),
            "volume24h": _decimal_to_json(self.volume24h),
            "abs_price24h_pcnt": _decimal_to_json(self.abs_price24h_pcnt),
            "spread_bps": _decimal_to_json(self.spread_bps),
            "depth_notional_top5": _decimal_to_json(self.depth_notional_top5),
        }


@dataclass(frozen=True)
class DailyUniverseSnapshot:
    selection_day_utc: str
    generated_at_utc: str
    selector_version: str
    max_symbols: int
    selected: tuple[UniverseSelection, ...] = field(default_factory=tuple)
    excluded: tuple[UniverseExclusion, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_day_utc": self.selection_day_utc,
            "generated_at_utc": self.generated_at_utc,
            "selector_version": self.selector_version,
            "max_symbols": self.max_symbols,
            "selected": [item.to_dict() for item in self.selected],
            "excluded": [item.to_dict() for item in self.excluded],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


class DailyUniverseSelector:
    """Deterministic once-daily universe selector for Bybit linear USDT perps."""

    def __init__(
        self,
        client: BybitPublicClient,
        *,
        max_symbols: int = 20,
        candidate_pool_multiplier: int = 4,
        orderbook_limit: int = 25,
        depth_levels: int = 5,
        max_spread_bps: Decimal | str = "20",
        min_depth_notional_top5: Decimal | str = "1000",
        selector_version: str = "bybit-public-universe-v1",
    ) -> None:
        self._client = client
        self._max_symbols = max_symbols
        self._candidate_pool_multiplier = candidate_pool_multiplier
        self._orderbook_limit = orderbook_limit
        self._depth_levels = depth_levels
        self._max_spread_bps = Decimal(max_spread_bps)
        self._min_depth_notional_top5 = Decimal(min_depth_notional_top5)
        self._selector_version = selector_version

    def build_snapshot(self, now: datetime | None = None) -> DailyUniverseSnapshot:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(UTC)

        instruments = self._client.list_linear_usdt_perpetual_instruments()
        tickers_payload = self._client.get_tickers(category="linear")
        tickers_result = tickers_payload.get("result", {})
        tickers_raw = tickers_result.get("list", [])
        if not isinstance(tickers_raw, list):
            raise BybitPublicError("ticker list must be an array")
        tickers = {row.get("symbol"): row for row in tickers_raw if isinstance(row, dict)}

        basic_eligible: list[dict[str, Any]] = []
        excluded: list[UniverseExclusion] = []

        for instrument in instruments:
            symbol = str(instrument.get("symbol", ""))
            reasons: list[str] = []
            if instrument.get("contractType") != "LinearPerpetual":
                reasons.append("not_linear_perpetual")
            if instrument.get("quoteCoin") != "USDT":
                reasons.append("not_usdt_quote")
            if instrument.get("settleCoin") != "USDT":
                reasons.append("not_usdt_settlement")
            if instrument.get("status") != "Trading":
                reasons.append("not_trading")
            if instrument.get("isPreListing") is True:
                reasons.append("prelisting")

            ticker = tickers.get(symbol)
            if ticker is None:
                reasons.append("missing_ticker")

            turnover = _decimal((ticker or {}).get("turnover24h")) if ticker else None
            volume = _decimal((ticker or {}).get("volume24h")) if ticker else None
            move = _decimal((ticker or {}).get("price24hPcnt")) if ticker else None
            if move is None and ticker is not None:
                move = _decimal((ticker or {}).get("change24h"))
            abs_move = abs(move) if move is not None else None

            if turnover is None:
                reasons.append("missing_turnover24h")
            if volume is None:
                reasons.append("missing_volume24h")
            if abs_move is None:
                reasons.append("missing_abs_price24h_pcnt")
            if reasons:
                excluded.append(
                    UniverseExclusion(
                        symbol=symbol,
                        reasons=tuple(dict.fromkeys(reasons)),
                        turnover24h=turnover,
                        volume24h=volume,
                        abs_price24h_pcnt=abs_move,
                    )
                )
                continue

            basic_eligible.append(
                {
                    "symbol": symbol,
                    "turnover24h": turnover,
                    "volume24h": volume,
                    "abs_price24h_pcnt": abs_move,
                }
            )

        # Bound REST calls: rank liquid/active ticker rows first, then inspect
        # depth only for the daily candidate pool. Calling orderbook once for
        # every listed contract is slow and can exceed public rate limits.
        basic_turnover = _rank_scores(
            [(item["symbol"], item["turnover24h"]) for item in basic_eligible], descending=True
        )
        basic_volume = _rank_scores([(item["symbol"], item["volume24h"]) for item in basic_eligible], descending=True)
        basic_move = _rank_scores(
            [(item["symbol"], item["abs_price24h_pcnt"]) for item in basic_eligible], descending=True
        )
        preliminary = sorted(
            basic_eligible,
            key=lambda item: (
                -(
                    Decimal("0.55") * basic_turnover[item["symbol"]]
                    + Decimal("0.15") * basic_volume[item["symbol"]]
                    + Decimal("0.30") * basic_move[item["symbol"]]
                ),
                item["symbol"],
            ),
        )
        pool_size = max(self._max_symbols, self._max_symbols * self._candidate_pool_multiplier)
        eligible: list[dict[str, Any]] = []
        for item in preliminary[:pool_size]:
            orderbook_payload = self._client.get_orderbook(
                symbol=item["symbol"], category="linear", limit=self._orderbook_limit
            )
            orderbook_result = orderbook_payload.get("result", {})
            if not isinstance(orderbook_result, dict):
                spread_bps, depth_notional_top5, orderbook_reason = None, None, "missing_orderbook"
            else:
                spread_bps, depth_notional_top5, orderbook_reason = _orderbook_metrics(
                    orderbook_result, depth_levels=self._depth_levels
                )
            reasons = [orderbook_reason] if orderbook_reason else []
            if spread_bps is None:
                reasons.append("missing_spread")
            elif spread_bps > self._max_spread_bps:
                reasons.append("spread_above_gate")
            if depth_notional_top5 is None:
                reasons.append("missing_depth")
            elif depth_notional_top5 < self._min_depth_notional_top5:
                reasons.append("depth_below_gate")
            if reasons:
                excluded.append(
                    UniverseExclusion(
                        symbol=item["symbol"],
                        reasons=tuple(dict.fromkeys(reasons)),
                        turnover24h=item["turnover24h"],
                        volume24h=item["volume24h"],
                        abs_price24h_pcnt=item["abs_price24h_pcnt"],
                        spread_bps=spread_bps,
                        depth_notional_top5=depth_notional_top5,
                    )
                )
            else:
                eligible.append({**item, "spread_bps": spread_bps, "depth_notional_top5": depth_notional_top5})
        for item in preliminary[pool_size:]:
            excluded.append(
                UniverseExclusion(
                    symbol=item["symbol"],
                    reasons=("outside_orderbook_candidate_pool",),
                    turnover24h=item["turnover24h"],
                    volume24h=item["volume24h"],
                    abs_price24h_pcnt=item["abs_price24h_pcnt"],
                )
            )

        turnover_scores = _rank_scores(
            [(item["symbol"], item["turnover24h"]) for item in eligible],
            descending=True,
        )
        volume_scores = _rank_scores(
            [(item["symbol"], item["volume24h"]) for item in eligible],
            descending=True,
        )
        move_scores = _rank_scores(
            [(item["symbol"], item["abs_price24h_pcnt"]) for item in eligible],
            descending=True,
        )
        depth_scores = _rank_scores(
            [(item["symbol"], item["depth_notional_top5"]) for item in eligible],
            descending=True,
        )
        spread_scores = _rank_scores(
            [(item["symbol"], item["spread_bps"]) for item in eligible],
            descending=False,
        )

        scored: list[tuple[Decimal, dict[str, Any]]] = []
        for item in eligible:
            symbol = item["symbol"]
            score = (
                Decimal("0.40") * turnover_scores[symbol]
                + Decimal("0.20") * volume_scores[symbol]
                + Decimal("0.15") * move_scores[symbol]
                + Decimal("0.20") * depth_scores[symbol]
                + Decimal("0.05") * spread_scores[symbol]
            )
            scored.append((score, item))

        scored.sort(key=lambda pair: (-pair[0], pair[1]["symbol"]))
        selected_rows: list[UniverseSelection] = []
        for rank, (score, item) in enumerate(scored[: self._max_symbols], start=1):
            selected_rows.append(
                UniverseSelection(
                    symbol=item["symbol"],
                    rank=rank,
                    score=score,
                    turnover24h=item["turnover24h"],
                    volume24h=item["volume24h"],
                    abs_price24h_pcnt=item["abs_price24h_pcnt"],
                    spread_bps=item["spread_bps"],
                    depth_notional_top5=item["depth_notional_top5"],
                )
            )

        for score, item in scored[self._max_symbols :]:
            excluded.append(
                UniverseExclusion(
                    symbol=item["symbol"],
                    reasons=("ranked_below_daily_cap",),
                    turnover24h=item["turnover24h"],
                    volume24h=item["volume24h"],
                    abs_price24h_pcnt=item["abs_price24h_pcnt"],
                    spread_bps=item["spread_bps"],
                    depth_notional_top5=item["depth_notional_top5"],
                )
            )

        notes = (
            "public REST only",
            "selection uses turnover24h, volume24h, abs(price24hPcnt), spread, and depth from public endpoints",
            "trade-count is not exposed by the public linear ticker or kline discovery endpoints, so it is not part of the selector",
            "no intraday additions after the daily snapshot",
        )
        return DailyUniverseSnapshot(
            selection_day_utc=current.date().isoformat(),
            generated_at_utc=current.isoformat(),
            selector_version=self._selector_version,
            max_symbols=self._max_symbols,
            selected=tuple(selected_rows),
            excluded=tuple(excluded),
            notes=notes,
        )


def _orderbook_metrics(
    orderbook_row: Mapping[str, Any],
    *,
    depth_levels: int,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    bids = orderbook_row.get("b")
    asks = orderbook_row.get("a")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        return None, None, "invalid_orderbook"

    best_bid = _decimal(bids[0][0] if len(bids[0]) >= 2 else None)
    best_ask = _decimal(asks[0][0] if len(asks[0]) >= 2 else None)
    if best_bid is None or best_ask is None:
        return None, None, "invalid_orderbook_prices"
    if best_ask <= best_bid:
        return None, None, "crossed_or_locked_book"

    mid = (best_bid + best_ask) / Decimal(2)
    spread_bps = ((best_ask - best_bid) / mid) * Decimal(10000)

    top_bids = bids[:depth_levels]
    top_asks = asks[:depth_levels]
    depth = Decimal(0)
    for side in (top_bids, top_asks):
        for level in side:
            if not isinstance(level, list) or len(level) < 2:
                return spread_bps, None, "invalid_orderbook_depth"
            price = _decimal(level[0])
            size = _decimal(level[1])
            if price is None or size is None:
                return spread_bps, None, "invalid_orderbook_depth"
            depth += price * size

    return spread_bps, depth, None
