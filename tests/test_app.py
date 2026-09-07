from sniper_paper.app import _parse_klines


def test_parse_klines_orders_oldest_first_and_excludes_forming() -> None:
    rows = [
        [120_000, "3", "4", "2", "3.5", "5", "17"],
        [60_000, "2", "3", "1", "2.5", "4", "10"],
        [0, "1", "2", "0.5", "1.5", "3", "4"],
    ]
    bars = _parse_klines("XUSDT", "1m", rows, 150_000)
    assert [item.opened_at_ms for item in bars] == [0, 60_000]
    assert bars[-1].close == 2.5
