from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from sniper_paper import stream as stream_mod
from sniper_paper.stream import public_topics


def test_public_topics_have_only_orderbook_and_trade() -> None:
    assert public_topics(["BTCUSDT"]) == ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"]
    assert all("private" not in topic for topic in public_topics(["ETHUSDT"]))


@pytest.mark.parametrize("symbol", ["", "btcUSDT", "BTC-USDT"])
def test_public_topics_reject_invalid_symbols(symbol: str) -> None:
    with pytest.raises(ValueError):
        public_topics([symbol])


def test_run_public_stream_uses_application_ping_and_skips_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []
    messages: list[dict[str, object]] = []
    stop = asyncio.Event()

    class FakeSocket:
        def __init__(self) -> None:
            self.recv_calls = 0

        async def send(self, payload: str) -> None:
            sent.append(json.loads(payload))

        async def recv(self) -> str:
            self.recv_calls += 1
            if self.recv_calls == 1:
                await asyncio.sleep(0.05)
                raise AssertionError("first recv should be cancelled by wait_for")
            if self.recv_calls == 2:
                return json.dumps({"op": "pong"})
            if self.recv_calls == 3:
                return json.dumps({"topic": "publicTrade.BTCUSDT", "data": []})
            raise AssertionError(f"unexpected recv call {self.recv_calls}")

    class FakeConnect:
        def __init__(self, socket: FakeSocket) -> None:
            self.socket = socket

        async def __aenter__(self) -> FakeSocket:
            return self.socket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    fake_socket = FakeSocket()
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: FakeConnect(fake_socket))
    monkeypatch.setattr(stream_mod, "PUBLIC_APP_PING_INTERVAL_S", 0.01)
    monkeypatch.setattr(stream_mod, "PUBLIC_APP_PING_TIMEOUT_S", 0.05)

    async def handler(message: dict, received_at_ms: int) -> None:
        messages.append(message)
        if message.get("topic") == "publicTrade.BTCUSDT":
            stop.set()

    asyncio.run(stream_mod.run_public_stream(["BTCUSDT"], handler, stop=stop, url="wss://example"))

    assert sent[0] == {"op": "subscribe", "args": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"]}
    assert sent[1] == {"op": "ping"}
    assert messages[0]["op"] == "connection"
    assert messages[-1]["topic"] == "publicTrade.BTCUSDT"
