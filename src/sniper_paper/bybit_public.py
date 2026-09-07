from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib import parse, request

JsonDict = dict[str, Any]
Transport = Callable[[str, str, Mapping[str, Any]], JsonDict]


class BybitPublicError(RuntimeError):
    """Raised when a public Bybit request fails or returns malformed data."""


def _clean_params(params: Mapping[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        cleaned[key] = str(value)
    return cleaned


@dataclass(frozen=True)
class HttpTransport:
    """Minimal GET-only JSON transport for Bybit public REST."""

    base_url: str = "https://api.bybit.com"
    timeout: float = 10.0
    user_agent: str = "sniper-paper/1.0"

    def __call__(self, method: str, path: str, params: Mapping[str, Any]) -> JsonDict:
        if method.upper() != "GET":
            raise BybitPublicError(f"unsupported public method: {method}")

        query = parse.urlencode(_clean_params(params))
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        req = request.Request(url, headers={"User-Agent": self.user_agent}, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - exercised indirectly
            raise BybitPublicError(f"public request failed for {path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise BybitPublicError(f"malformed response from {path}: expected object")
        return payload


class BybitPublicClient:
    """GET-only wrapper for the Bybit public market-data endpoints we use."""

    def __init__(self, transport: Transport | None = None):
        self._transport = transport or HttpTransport()

    def _get(self, path: str, **params: Any) -> JsonDict:
        payload = self._transport("GET", path, params)
        if not isinstance(payload, dict):
            raise BybitPublicError(f"malformed payload for {path}: expected object")
        ret_code = payload.get("retCode")
        if ret_code not in (0, "0", None):
            raise BybitPublicError(f"bybit error for {path}: retCode={ret_code!r}, retMsg={payload.get('retMsg')!r}")
        result = payload.get("result")
        if result is not None and not isinstance(result, dict):
            raise BybitPublicError(f"malformed payload for {path}: result must be object")
        return payload

    def get_instruments_info(
        self,
        *,
        category: str = "linear",
        symbol: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> JsonDict:
        return self._get(
            "/v5/market/instruments-info",
            category=category,
            symbol=symbol,
            limit=limit,
            cursor=cursor,
        )

    def get_tickers(self, *, category: str = "linear", symbol: str | None = None) -> JsonDict:
        return self._get("/v5/market/tickers", category=category, symbol=symbol)

    def get_orderbook(
        self,
        *,
        symbol: str,
        category: str = "linear",
        limit: int | None = None,
    ) -> JsonDict:
        return self._get("/v5/market/orderbook", category=category, symbol=symbol, limit=limit)

    def get_kline(
        self,
        *,
        symbol: str,
        interval: str,
        category: str = "linear",
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
    ) -> JsonDict:
        return self._get(
            "/v5/market/kline",
            category=category,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            limit=limit,
        )

    def list_linear_usdt_perpetual_instruments(self) -> list[dict[str, Any]]:
        cursor: str | None = None
        instruments: list[dict[str, Any]] = []
        while True:
            payload = self.get_instruments_info(category="linear", limit=1000, cursor=cursor)
            result = payload.get("result", {})
            page = result.get("list", [])
            if not isinstance(page, list):
                raise BybitPublicError("instrument list must be an array")
            for item in page:
                if isinstance(item, dict):
                    instruments.append(item)
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return instruments
