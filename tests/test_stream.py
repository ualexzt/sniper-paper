import pytest

from sniper_paper.stream import public_topics


def test_public_topics_have_only_orderbook_and_trade() -> None:
    assert public_topics(["BTCUSDT"]) == ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"]
    assert all("private" not in topic for topic in public_topics(["ETHUSDT"]))


@pytest.mark.parametrize("symbol", ["", "btcUSDT", "BTC-USDT"])
def test_public_topics_reject_invalid_symbols(symbol: str) -> None:
    with pytest.raises(ValueError):
        public_topics([symbol])
