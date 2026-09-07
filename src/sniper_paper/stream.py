"""Reconnect-safe public Bybit WebSocket stream; it has no auth/order path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
MessageHandler = Callable[[dict, int], Awaitable[None]]


def public_topics(symbols: Sequence[str]) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        if not symbol or not symbol.isalnum() or symbol.upper() != symbol:
            raise ValueError(f"invalid symbol: {symbol!r}")
        result.extend((f"orderbook.50.{symbol}", f"publicTrade.{symbol}"))
    if not result:
        raise ValueError("at least one symbol is required")
    return result


async def run_public_stream(
    symbols: Sequence[str],
    handler: MessageHandler,
    *,
    stop: asyncio.Event | None = None,
    url: str = PUBLIC_LINEAR_WS,
) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    topics = public_topics(symbols)
    stop = stop or asyncio.Event()
    attempt = 0
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10, max_size=8_000_000) as socket:
                await socket.send(json.dumps({"op": "subscribe", "args": topics}))
                await handler({"op": "connection", "state": "connected"}, time.time_ns() // 1_000_000)
                attempt = 0
                while not stop.is_set():
                    raw = await socket.recv()
                    received_at_ms = time.time_ns() // 1_000_000
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise TypeError("public stream payload must be an object")
                    await handler(message, received_at_ms)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionClosed, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            with contextlib.suppress(Exception):
                await handler(
                    {"op": "connection", "state": "disconnected", "error": str(exc)},
                    time.time_ns() // 1_000_000,
                )
            delay = min(2**attempt, 15)
            attempt += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
