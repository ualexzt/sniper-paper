import json
from pathlib import Path
from urllib import error, request

import pytest

from sniper_paper.storage import Journal
from sniper_paper.web import dashboard_server, start_dashboard


def test_dashboard_http_is_read_only(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    server, thread = start_dashboard(journal, port=0)
    host, port = server.server_address
    try:
        with pytest.raises(error.HTTPError) as health:
            request.urlopen(f"http://{host}:{port}/healthz")
        assert health.value.code == 503
        assert b"Sniper Paper" in request.urlopen(f"http://{host}:{port}/").read()
        with pytest.raises(error.HTTPError) as caught:
            request.urlopen(request.Request(f"http://{host}:{port}/", method="POST"))
        assert caught.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_nonloopback_bind_requires_explicit_opt_in(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="explicit"):
        dashboard_server(journal, "0.0.0.0", 0)


def test_market_endpoint_uses_read_only_provider(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "paper.db")
    calls = []

    def provider(symbol: str, timeframe: str) -> dict:
        calls.append((symbol, timeframe))
        return {"symbol": symbol, "timeframe": timeframe, "bars": []}

    server, thread = start_dashboard(journal, port=0, market_provider=provider)
    host, port = server.server_address
    try:
        payload = json.loads(request.urlopen(f"http://{host}:{port}/api/market?symbol=BTCUSDT&timeframe=15m").read())
        assert payload["symbol"] == "BTCUSDT"
        assert calls == [("BTCUSDT", "15m")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
