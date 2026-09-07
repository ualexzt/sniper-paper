from __future__ import annotations

from sniper_paper.bybit_public import BybitPublicClient


class ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, method: str, path: str, params: dict[str, object]):
        self.calls.append((method, path, dict(params)))
        if path == "/v5/market/instruments-info":
            cursor = params.get("cursor")
            if cursor is None:
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
                            },
                            {
                                "symbol": "ETHUSDT",
                                "contractType": "LinearPerpetual",
                                "status": "PreLaunch",
                                "baseCoin": "ETH",
                                "quoteCoin": "USDT",
                                "settleCoin": "USDT",
                            },
                        ],
                        "nextPageCursor": "cursor-2",
                    },
                }
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "XRPUSDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "XRP",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                        },
                        {
                            "symbol": "ADAUSDC",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": "ADA",
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
                            "turnover24h": "100",
                            "volume24h": "10",
                            "price24hPcnt": "0.01",
                            "bid1Price": "99.9",
                            "ask1Price": "100.1",
                            "bid1Size": "1",
                            "ask1Size": "1",
                        },
                        {
                            "symbol": "XRPUSDT",
                            "turnover24h": "200",
                            "volume24h": "20",
                            "price24hPcnt": "0.02",
                            "bid1Price": "0.499",
                            "ask1Price": "0.501",
                            "bid1Size": "1",
                            "ask1Size": "1",
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
                        "b": [["99.9", "2"], ["99.8", "3"], ["99.7", "4"]],
                        "a": [["100.1", "2"], ["100.2", "3"], ["100.3", "4"]],
                    },
                }
            if symbol == "XRPUSDT":
                return {
                    "retCode": 0,
                    "result": {
                        "b": [["0.499", "10"], ["0.498", "10"]],
                        "a": [["0.501", "10"], ["0.502", "10"]],
                    },
                }
        raise AssertionError(f"unexpected call: {method} {path} {params}")


def test_public_client_paginates_linear_instruments_and_stays_get_only():
    transport = ScriptedTransport()
    client = BybitPublicClient(transport=transport)

    instruments = client.list_linear_usdt_perpetual_instruments()

    assert [item["symbol"] for item in instruments] == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDC"]
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1] == "/v5/market/instruments-info"
    assert transport.calls[1][2]["cursor"] == "cursor-2"


def test_public_client_exposes_tickers_orderbook_and_kline_getters():
    calls: list[tuple[str, str, dict[str, object]]] = []

    def transport(method: str, path: str, params: dict[str, object]):
        calls.append((method, path, dict(params)))
        return {"retCode": 0, "result": {"list": [], "b": [], "a": []}}

    client = BybitPublicClient(transport=transport)
    client.get_tickers(category="linear")
    client.get_orderbook(symbol="BTCUSDT", limit=25)
    client.get_kline(symbol="BTCUSDT", interval="1", limit=2)

    assert [call[1] for call in calls] == [
        "/v5/market/tickers",
        "/v5/market/orderbook",
        "/v5/market/kline",
    ]
    assert all(call[0] == "GET" for call in calls)
