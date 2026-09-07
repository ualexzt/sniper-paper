"""Reconnect-safe public Bybit WebSocket stream; it has no auth/order path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_APP_PING_INTERVAL_S = 20.0
PUBLIC_APP_PING_TIMEOUT_S = 10.0
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


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _is_pong(message: dict) -> bool:
    return message.get("op") == "pong" or message.get("ret_msg") == "pong"


def _parse_message(raw: str | bytes) -> dict:
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise TypeError("public stream payload must be an object")
    return message


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
            async with websockets.connect(url, ping_interval=None, ping_timeout=None, max_size=8_000_000) as socket:
                await socket.send(json.dumps({"op": "subscribe", "args": topics}))
                await handler(
                    {"op": "connection", "state": "connected", "ready": True, "gap": False},
                    _now_ms(),
                )
                attempt = 0
                last_activity_ms = _now_ms()
                while not stop.is_set():
                    idle_timeout = PUBLIC_APP_PING_INTERVAL_S - ((_now_ms() - last_activity_ms) / 1000.0)
                    if idle_timeout <= 0:
                        idle_timeout = 0.1
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=idle_timeout)
                    except TimeoutError:
                        await socket.send(json.dumps({"op": "ping"}))
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=PUBLIC_APP_PING_TIMEOUT_S)
                        except TimeoutError as exc:
                            raise TimeoutError("application ping timed out") from exc
                    received_at_ms = _now_ms()
                    message = _parse_message(raw)
                    last_activity_ms = received_at_ms
                    if _is_pong(message):
                        continue
                    await handler(message, received_at_ms)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionClosed, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            with contextlib.suppress(Exception):
                await handler(
                    {"op": "connection", "state": "disconnected", "error": str(exc)},
                    _now_ms(),
                )
            delay = min(2**attempt, 15)
            attempt += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
