from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sniper_paper.bybit_public import BybitPublicClient
from sniper_paper.universe import DailyUniverseSelector


class UniverseTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, method: str, path: str, params: dict[str, object]):
        self.calls.append((method, path, dict(params)))
        if path == "/v5/market/instruments-info":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "BTC",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0.5"},
                            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                        },
                        {
                            "symbol": "ETHUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "ETH",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0.1"},
                            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"},
                        },
                        {
                            "symbol": "DOGEUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "DOGE",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0.0001"},
                            "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1"},
                        },
                        {
                            "symbol": "SOLUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "SOL",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0.01"},
                            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"},
                        },
                        {
                            "symbol": "BADUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "BAD",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                        },
                        {
                            "symbol": "BROKENUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "BROKEN",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0"},
                            "lotSizeFilter": {"qtyStep": "-1", "minOrderQty": "abc"},
                        },
                        {
                            "symbol": "MATICUSDC",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "MATIC",
                            "quoteCoin": "USDC",
                            "settleCoin": "USDC",
                        },
                    ],
                    "nextPageCursor": "",
                },
            }
        if path == "/v5/market/tickers":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "turnover24h": "2000000",
                            "volume24h": "200",
                            "price24hPcnt": "0.012",
                        },
                        {
                            "symbol": "ETHUSDT",
                            "turnover24h": "2000000",
                            "volume24h": "210",
                            "price24hPcnt": "0.030",
                        },
                        {
                            "symbol": "DOGEUSDT",
                            "turnover24h": "500000",
                            "volume24h": "1000",
                            "price24hPcnt": "0.120",
                        },
                        {
                            "symbol": "SOLUSDT",
                            "turnover24h": "100000",
                            "volume24h": "50",
                            "price24hPcnt": "0.010",
                        },
                        {
                            "symbol": "BADUSDT",
                            "turnover24h": "90000",
                            "volume24h": "90",
                            "price24hPcnt": "0.020",
                        },
                        {
                            "symbol": "BROKENUSDT",
                            "turnover24h": "80000",
                            "volume24h": "80",
                            "price24hPcnt": "0.025",
                        },
                    ]
                },
            }
        if path == "/v5/market/orderbook":
            symbol = params["symbol"]
            if symbol == "BTCUSDT":
                return {
                    "retCode": 0,
                    "result": {
                        "b": [["100", "10"], ["99.5", "20"], ["99", "30"]],
                        "a": [["100.1", "10"], ["100.2", "20"], ["100.3", "30"]],
                    },
                }
            if symbol == "ETHUSDT":
                return {
                    "retCode": 0,
                    "result": {
                        "b": [["100", "2"], ["99.9", "2"], ["99.8", "2"]],
                        "a": [["100.4", "2"], ["100.5", "2"], ["100.6", "2"]],
                    },
                }
            if symbol == "DOGEUSDT":
                return {
                    "retCode": 0,
                    "result": {
                        "b": [["0.1", "1"]],
                        "a": [["0.10005", "1"]],
                    },
                }
            if symbol == "SOLUSDT":
                return {
                    "retCode": 0,
                    "result": {
                        "b": [["10", "1"], ["9.9", "1"]],
                        "a": [["10.9", "1"], ["11", "1"]],
                    },
                }
        raise AssertionError(f"unexpected call: {method} {path} {params}")


def test_daily_selector_ranks_from_public_ticker_and_orderbook_fields_and_records_exclusions():
    client = BybitPublicClient(transport=UniverseTransport())
    selector = DailyUniverseSelector(
        client,
        max_symbols=2,
        orderbook_limit=25,
        depth_levels=3,
        max_spread_bps="1000",
        min_depth_notional_top5="0",
    )

    snapshot = selector.build_snapshot(now=datetime(2026, 9, 7, 0, 5, tzinfo=UTC))

    assert snapshot.selection_day_utc == "2026-09-07"
    assert [item.symbol for item in snapshot.selected] == ["BTCUSDT", "ETHUSDT"]
    assert snapshot.selected[0].rank == 1
    assert snapshot.selected[0].spread_bps < snapshot.selected[1].spread_bps
    assert snapshot.selected[0].tick_size == Decimal("0.5")
    assert snapshot.selected[0].qty_step == Decimal("0.001")
    assert snapshot.selected[0].min_order_qty == Decimal("0.001")
    excluded_reasons = {item.symbol: item.reasons for item in snapshot.excluded}
    assert "ranked_below_daily_cap" in excluded_reasons["DOGEUSDT"]
    assert "ranked_below_daily_cap" in excluded_reasons["SOLUSDT"]
    assert "missing_tickSize" in excluded_reasons["BADUSDT"]
    assert "invalid_tickSize" in excluded_reasons["BROKENUSDT"]
    assert "invalid_qtyStep" in excluded_reasons["BROKENUSDT"]
    assert "invalid_minOrderQty" in excluded_reasons["BROKENUSDT"]
    excluded_payload = {item["symbol"]: item for item in snapshot.to_dict()["excluded"]}
    assert excluded_payload["BADUSDT"]["tick_size"] is None
    assert excluded_payload["BADUSDT"]["qty_step"] is None
    assert excluded_payload["BADUSDT"]["min_order_qty"] is None
    payload = snapshot.to_dict()
    assert payload["selected"][0]["symbol"] == "BTCUSDT"
    assert payload["selected"][0]["tick_size"] == "0.5"
    assert "trade-count" in payload["notes"][2]


def test_daily_snapshot_is_json_serializable():
    client = BybitPublicClient(transport=UniverseTransport())
    selector = DailyUniverseSelector(
        client,
        max_symbols=2,
        orderbook_limit=25,
        depth_levels=3,
        max_spread_bps="1000",
        min_depth_notional_top5="0",
    )

    snapshot = selector.build_snapshot(now=datetime(2026, 9, 7, 0, 5, tzinfo=UTC))

    json_blob = snapshot.to_json()
    assert '"selection_day_utc": "2026-09-07"' in json_blob
    assert '"ranked_below_daily_cap"' in json_blob
